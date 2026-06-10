"""Entry point: FastAPI + hypercorn + Restate-native A2A server.

Deployment model (mirroring the Restate reimbursement example):
  • This process exposes Restate handler endpoints at  /restate/v1
  • A Restate server (default localhost:8080) registers this app and acts as
    the durable ingress for A2A clients.
  • The agent card URL therefore points at the Restate server, not this process.

To run locally:
  1. Start this app:      python -m app  (default port 9081)
  2. Start Restate:       restate-server
  3. Register services:   restate deployments register http://localhost:9081/restate/v1
  4. A2A clients call:   http://localhost:8080/ReimbursementA2AServer/process_request
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import hypercorn.asyncio
import hypercorn.config
import restate
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from google.protobuf import json_format

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)

from app.agent import ReimbursementAgent, reimbursement_service, payment_service
from app.agent import invoke as reimbursement_invoke

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_part(text: str) -> dict:
    return {'kind': 'text', 'text': text}


def _extract_query(message: dict) -> str:
    for part in message.get('parts', []):
        if isinstance(part, dict) and 'text' in part:
            return part['text']
    return ''


def _error_response(req_id: int | str | None, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}}


# ── Restate VirtualObject: one instance per task_id ──────────────────────────
# Stores task state in Restate K/V so it survives process restarts.

task_object = restate.VirtualObject('ReimbursementTaskObject')
_TASK_KEY = 'task'


@task_object.handler()
async def handle_send_message(ctx: restate.ObjectContext, req: dict) -> dict:
    """Handle a message/send JSON-RPC request for a specific task (ctx.key = task_id)."""
    task_id = ctx.key()
    params = req.get('params', {})
    message = params.get('message', {})

    # Ensure context_id exists – reuse task_id if the client didn't supply one.
    context_id: str = message.get('contextId') or task_id
    message['contextId'] = context_id

    query = _extract_query(message)
    logger.info('handle_send_message task=%s context=%s', task_id, context_id)

    # Upsert the A2A Task record.
    task: dict = await ctx.get(_TASK_KEY, type_hint=dict) or {
        'id': task_id,
        'contextId': context_id,
        'kind': 'task',
        'status': {'state': 'submitted', 'timestamp': _now_iso()},
        'history': [],
    }
    task.setdefault('history', []).append(message)
    ctx.set(_TASK_KEY, task)

    # Delegate to the ReimbursementService virtual object (keyed by context_id so
    # LangGraph MemorySaver can accumulate conversation history per session).
    result: dict = await ctx.object_call(reimbursement_invoke, key=context_id, arg=query)

    # Map the agent result back to A2A task state.
    if result.get('require_user_input'):
        task['status'] = {
            'state': 'input-required',
            'timestamp': _now_iso(),
            'message': {
                'kind': 'message',
                'role': 'agent',
                'messageId': str(ctx.uuid()),
                'parts': [_text_part(result['content'])],
            },
        }
    elif result.get('is_task_complete'):
        task['artifacts'] = [
            {
                'kind': 'artifact',
                'artifactId': str(ctx.uuid()),
                'parts': [_text_part(result['content'])],
            }
        ]
        task['status'] = {'state': 'completed', 'timestamp': _now_iso()}
    else:
        task['status'] = {
            'state': 'failed',
            'timestamp': _now_iso(),
            'message': {
                'kind': 'message',
                'role': 'agent',
                'messageId': str(ctx.uuid()),
                'parts': [_text_part(result.get('content', 'Unknown error'))],
            },
        }
    ctx.set(_TASK_KEY, task)

    return {'jsonrpc': '2.0', 'id': req.get('id'), 'result': task}


@task_object.handler(kind='shared')
async def get_task_state(ctx: restate.ObjectSharedContext, req: dict) -> dict:
    """Return the stored task (shared = read-only, can run concurrently)."""
    task = await ctx.get(_TASK_KEY, type_hint=dict)
    if task is None:
        return _error_response(req.get('id'), -32001, 'Task not found')
    history_length: int = req.get('params', {}).get('historyLength', 0)
    result = dict(task)
    if history_length > 0:
        result['history'] = task.get('history', [])[-history_length:]
    else:
        result['history'] = []
    return {'jsonrpc': '2.0', 'id': req.get('id'), 'result': result}


@task_object.handler()
async def cancel_task_handler(ctx: restate.ObjectContext, req: dict) -> dict:
    """Mark a task as canceled."""
    task = await ctx.get(_TASK_KEY, type_hint=dict)
    if task is None:
        return _error_response(req.get('id'), -32001, 'Task not found')
    task['status'] = {'state': 'canceled', 'timestamp': _now_iso()}
    ctx.set(_TASK_KEY, task)
    return {'jsonrpc': '2.0', 'id': req.get('id'), 'result': task}


# ── Restate Service: stateless JSON-RPC router ───────────────────────────────

a2a_service = restate.Service('ReimbursementA2AServer')


@a2a_service.handler()
async def process_request(ctx: restate.Context, req: dict) -> dict:
    """Route an incoming A2A JSON-RPC request to the appropriate task handler."""
    method = req.get('method')
    req_id = req.get('id')

    if method == 'message/send':
        params = req.get('params', {})
        message = params.get('message', {})
        # Assign a stable task_id if the client didn't provide one.
        task_id: str = message.get('taskId') or str(ctx.uuid())
        message['taskId'] = task_id
        return await ctx.object_call(
            handle_send_message,
            key=task_id,
            arg=req,
        )

    elif method == 'tasks/get':
        task_id = req.get('params', {}).get('id')
        if not task_id:
            return _error_response(req_id, -32602, "Missing required param 'id'")
        return await ctx.object_call(get_task_state, key=task_id, arg=req)

    elif method == 'tasks/cancel':
        task_id = req.get('params', {}).get('id')
        if not task_id:
            return _error_response(req_id, -32602, "Missing required param 'id'")
        return await ctx.object_call(cancel_task_handler, key=task_id, arg=req)

    else:
        return _error_response(req_id, -32601, f"Method not found: {method!r}")


# ── main ─────────────────────────────────────────────────────────────────────

class _MissingEnvError(Exception):
    pass


def main() -> None:
    try:
        if not os.getenv('LITELLM_BASE_URL'):
            raise _MissingEnvError('LITELLM_BASE_URL environment variable not set.')
        if not os.getenv('LITELLM_MODEL'):
            raise _MissingEnvError('LITELLM_MODEL environment variable not set.')

        host = os.getenv('AGENT_HOST', 'localhost')
        port = int(os.getenv('AGENT_PORT', '9081'))
        restate_host = os.getenv('RESTATE_HOST', 'http://localhost:8080')

        # Build the A2A agent card.  The URL points at the Restate ingress so
        # clients enjoy durable, exactly-once delivery.
        agent_card = AgentCard(
            name='Reimbursement Agent',
            description='Handles employee expense reimbursement requests with durable workflow and human-in-the-loop approval',
            supported_interfaces=[
                AgentInterface(
                    url=f'{restate_host}/ReimbursementA2AServer/process_request',
                    protocol_binding='JSONRPC',
                )
            ],
            version='1.0.0',
            default_input_modes=ReimbursementAgent.SUPPORTED_CONTENT_TYPES,
            default_output_modes=ReimbursementAgent.SUPPORTED_CONTENT_TYPES,
            capabilities=AgentCapabilities(streaming=False, push_notifications=False),
            skills=[
                AgentSkill(
                    id='process_reimbursement',
                    name='Reimbursement Processing',
                    description='Handle employee expense reimbursement requests end-to-end',
                    tags=['reimbursement', 'expense', 'finance', 'workflow'],
                    examples=[
                        'I need to submit a reimbursement for a $50 client lunch on Dec 1st.',
                        'Please reimburse my $200 conference registration fee.',
                    ],
                )
            ],
        )
        agent_card_json = json.loads(json_format.MessageToJson(agent_card))

        app = FastAPI()

        @app.get('/.well-known/agent.json')
        async def get_agent_card():
            return JSONResponse(content=agent_card_json)

        # Mount all Restate services: the A2A router, per-task state object,
        # the durable reimbursement agent, and the end-of-month payment service.
        app.mount(
            '/restate/v1',
            restate.app([a2a_service, task_object, reimbursement_service, payment_service]),
        )

        conf = hypercorn.config.Config()
        conf.bind = [f'{host}:{port}']
        logger.info('Reimbursement Agent running at http://%s:%s', host, port)
        logger.info('  Agent card : http://%s:%s/.well-known/agent.json', host, port)
        logger.info('  Restate services : http://%s:%s/restate/v1', host, port)
        logger.info('  A2A URL (via Restate ingress) : %s/ReimbursementA2AServer/process_request', restate_host)
        logger.info('Register with: restate deployments register http://%s:%s/restate/v1', host, port)

        asyncio.run(hypercorn.asyncio.serve(app, conf))

    except _MissingEnvError as exc:
        logger.error('Configuration error: %s', exc)
        sys.exit(1)
    except Exception as exc:
        logger.error('Server startup failed: %s', exc)
        sys.exit(1)


if __name__ == '__main__':
    main()
