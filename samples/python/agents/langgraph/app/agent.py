import calendar
import contextvars
import json
import logging
import os
import uuid as _uuid_module
from collections.abc import AsyncIterable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import restate

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logging.getLogger('LiteLLM').setLevel(logging.INFO)
logger = logging.getLogger(__name__)
memory = MemorySaver()

# Propagates the Restate ObjectContext into async LangGraph tools.
# Set by the `invoke` handler before running the agent; reset in a finally block.
_restate_ctx: contextvars.ContextVar[restate.ObjectContext | None] = contextvars.ContextVar(
    '_restate_ctx', default=None
)

# ── Domain model ─────────────────────────────────────────────────────────────


class Reimbursement(BaseModel):
    request_id: str
    date: str
    amount: float
    purpose: str


# ── Backoffice side-effects (wrapped in ctx.run_typed to be durable) ─────────


def _backoffice_submit_request(request_id: str, callback_id: str) -> None:
    """Print approval instructions so a human can resolve the awakeable."""
    print(
        '=' * 60,
        f'\n  Approval required for request {request_id}\n',
        '  To approve:\n',
        f'    curl -X POST localhost:8080/restate/awakeables/{callback_id}/resolve'
        ' -H "Content-Type: application/json" -d \'true\'\n',
        '  To reject:\n',
        f'    curl -X POST localhost:8080/restate/awakeables/{callback_id}/resolve'
        ' -H "Content-Type: application/json" -d \'false\'\n',
        '=' * 60,
    )


def _backoffice_email_employee(request_id: str, approved: bool) -> None:
    status = 'approved' if approved else 'rejected'
    print(f'[Backoffice] Reimbursement request {request_id} was {status}.')


def _end_of_month(time_now: float) -> timedelta:
    now = datetime.fromtimestamp(time_now, tz=timezone.utc)
    last_day = calendar.monthrange(now.year, now.month)[1]
    end = datetime(now.year, now.month, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
    return timedelta(seconds=(end - now).total_seconds())


# ── Payment service (scheduled at end-of-month) ───────────────────────────────

payment_service = restate.Service('PaymentService')


@payment_service.handler()
async def handle_payment(_ctx: restate.Context, req: Reimbursement) -> None:
    """Process a reimbursement payment (called by Restate at end of month)."""
    logger.info(
        '[PaymentService] Processing $%.2f for request %s', req.amount, req.request_id
    )
    # Integrate with your payment provider here.


# ── LangGraph tools ───────────────────────────────────────────────────────────


@tool
def create_request_form(
    date: str = '',
    amount: str = '',
    purpose: str = '',
) -> dict:
    """Create a new reimbursement request form.

    Args:
        date: Date of the transaction (empty string if not yet known).
        amount: Dollar amount (empty string if not yet known).
        purpose: Business justification (empty string if not yet known).

    Returns:
        A dict with a generated request_id and the supplied field values
        (or placeholder strings for missing fields).
    """
    ctx = _restate_ctx.get()
    request_id = str(ctx.uuid()) if ctx else str(_uuid_module.uuid4())
    return {
        'request_id': request_id,
        'date': date or '<transaction date>',
        'amount': amount or '<transaction dollar amount>',
        'purpose': purpose or '<business justification / purpose of transaction>',
    }


@tool
def return_form(form_data: dict, instructions: str = '') -> str:
    """Return a structured JSON form to the user for review or completion.

    Args:
        form_data: The form dict returned by create_request_form.
        instructions: Optional guidance for the user (e.g. which fields are missing).

    Returns:
        A JSON string the client renders as a fillable form.
    """
    return json.dumps(
        {
            'type': 'form',
            'form': Reimbursement.model_json_schema(),
            'form_data': form_data,
            'instructions': instructions,
        }
    )


@tool
async def reimburse(
    request_id: str,
    date: str,
    amount: float,
    purpose: str,
) -> dict:
    """Submit a validated reimbursement request for approval and scheduled payment.

    Amounts ≤ $100 are auto-approved.  Amounts > $100 pause execution via a
    Restate awakeable until a human resolves the callback (approve/reject).
    On approval the payment is scheduled for end-of-month via Restate's
    durable timer API.

    Args:
        request_id: ID from create_request_form.
        date: Transaction date.
        amount: Dollar amount.
        purpose: Business justification.

    Returns:
        Dict with 'status': 'approved' | 'rejected' and 'request_id'.
    """
    ctx = _restate_ctx.get()

    if ctx and amount > 100.0:
        # Human-in-the-loop: pause until a human resolves the awakeable.
        callback_id, callback_promise = ctx.awakeable(type_hint=bool)
        await ctx.run_typed(
            'Request approval',
            _backoffice_submit_request,
            request_id=request_id,
            callback_id=callback_id,
        )
        approved: bool = await callback_promise
    else:
        approved = True

    if ctx:
        await ctx.run_typed(
            'Notify employee',
            _backoffice_email_employee,
            request_id=request_id,
            approved=approved,
        )

    if not approved:
        return {'status': 'rejected', 'request_id': request_id}

    if ctx:
        # Schedule the actual payment for end of month (durable timer).
        time_now: float = await ctx.time()
        ctx.service_send(
            handle_payment,
            arg=Reimbursement(
                request_id=request_id,
                date=date,
                amount=amount,
                purpose=purpose,
            ),
            send_delay=_end_of_month(time_now),
        )

    return {'status': 'approved', 'request_id': request_id}


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM_INSTRUCTION = """
You are an agent who handles the reimbursement process for employees.

When you receive a reimbursement request, first create a new request form with
create_request_form(). Only populate a field if the user explicitly provided the
value; otherwise pass an empty string so the placeholder is shown.
  1. 'Date' — the date of the transaction.
  2. 'Amount' — the dollar amount of the transaction.
  3. 'Business Justification/Purpose' — the reason for the reimbursement.

Once the form is created, immediately call return_form() with the form data and
return its result to the user.
If you need to ask the user for missing information, always start your response
with "MISSING_INFO:".  Do NOT change this prefix.

Once the user returns a filled-out form, verify all required fields are present:
  1. 'Date' — must be a real date.
  2. 'Amount' — must be a positive dollar amount.
  3. 'Business Justification/Purpose' — must describe the item / expense.

If any field is still missing, call return_form() again highlighting the gaps.

For a complete and valid form, call reimburse() to process the request.
Include the request_id and the final status in your response to the user.
"""

_FORMAT_INSTRUCTION = (
    'Set response status to input_required if the user needs to provide more information. '
    'Set response status to error if there is an error while processing the request. '
    'Set response status to completed if the request is complete.'
)


class ResponseFormat(BaseModel):
    """Structured response format for the reimbursement agent."""

    status: Literal['input_required', 'completed', 'error'] = 'input_required'
    message: str


# ── Agent ─────────────────────────────────────────────────────────────────────


class ReimbursementAgent:
    """LangGraph ReAct agent that processes employee reimbursement requests."""

    SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']

    def __init__(self) -> None:
        model_name = os.getenv('LITELLM_MODEL', 'gpt-4o-mini')
        base_url = os.getenv('LITELLM_BASE_URL', 'http://localhost:4000')
        if not model_name.startswith('openai/'):
            model_name = f'openai/{model_name}'
        logger.info(
            'Initializing ReimbursementAgent model=%s base_url=%s', model_name, base_url
        )
        self.model = ChatLiteLLM(
            model=model_name,
            api_key=os.getenv('LITELLM_API_KEY', 'EMPTY'),
            api_base=base_url,
            temperature=0,
        )
        self.tools = [create_request_form, return_form, reimburse]
        self.graph = create_react_agent(
            self.model,
            tools=self.tools,
            checkpointer=memory,
            prompt=_SYSTEM_INSTRUCTION,
            response_format=(_FORMAT_INSTRUCTION, ResponseFormat),
        )

    async def stream(self, query: str, context_id: str) -> AsyncIterable[dict[str, Any]]:
        inputs = {'messages': [('user', query)]}
        config = {'configurable': {'thread_id': context_id}}

        # Use astream so that async tools (reimburse) run in the current event loop
        # without trying to start a nested one.
        async for item in self.graph.astream(inputs, config, stream_mode='values'):
            message = item['messages'][-1]
            if isinstance(message, AIMessage) and message.tool_calls:
                yield {
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': 'Processing reimbursement request...',
                }
            elif isinstance(message, ToolMessage):
                yield {
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': 'Reviewing reimbursement details...',
                }

        yield self.get_agent_response(config)

    def get_agent_response(self, config: dict) -> dict[str, Any]:
        current_state = self.graph.get_state(config)
        structured_response = current_state.values.get('structured_response')
        if structured_response and isinstance(structured_response, ResponseFormat):
            if structured_response.status == 'input_required':
                return {
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.message,
                }
            if structured_response.status == 'error':
                return {
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.message,
                }
            if structured_response.status == 'completed':
                return {
                    'is_task_complete': True,
                    'require_user_input': False,
                    'content': structured_response.message,
                }
        return {
            'is_task_complete': False,
            'require_user_input': True,
            'content': 'We are unable to process your request at the moment. Please try again.',
        }


# ── Restate VirtualObject ─────────────────────────────────────────────────────
# One virtual object instance per conversation context_id.
# The Restate runtime provides exactly-once execution and durable state.

_agent_instance: ReimbursementAgent | None = None


def _get_agent() -> ReimbursementAgent:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = ReimbursementAgent()
    return _agent_instance


reimbursement_service = restate.VirtualObject('ReimbursementService')


@reimbursement_service.handler()
async def invoke(ctx: restate.ObjectContext, query: str) -> dict:
    """Run the reimbursement agent durably (ctx.key = conversation context_id).

    Propagates the Restate context into async tools via _restate_ctx so they
    can use awakeables and durable timers.
    """
    agent = _get_agent()
    session_id = ctx.key()
    logger.info('ReimbursementService.invoke session=%s query=%.80s', session_id, query)

    token = _restate_ctx.set(ctx)
    try:
        final_result: dict | None = None
        async for item in agent.stream(query, session_id):
            final_result = item
        return final_result or {
            'is_task_complete': False,
            'require_user_input': True,
            'content': 'Unable to process request.',
        }
    finally:
        _restate_ctx.reset(token)
