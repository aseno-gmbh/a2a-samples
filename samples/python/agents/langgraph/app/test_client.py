import asyncio
import logging
from uuid import uuid4

from a2a.client import create_client
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest, TaskState


# The agent card lives on the app server.  The card itself contains the A2A
# interface URL pointing at the Restate ingress (default :8080), so message
# sends are automatically routed through Restate for durable execution.
BASE_URL = 'http://localhost:9081'


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    client = await create_client(BASE_URL)
    logger.info('A2A client initialized.')

    # ── One-shot: complete request in a single message ────────────────────────
    request = SendMessageRequest(
        message=Message(
            message_id=uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(text='I need to reimburse $50 for a client lunch on 2024-12-01. Purpose: business development.')],
        )
    )
    async for chunk in client.send_message(request):
        print(chunk)

    # ── Multi-turn: first message is incomplete, agent asks for missing fields ─
    first_request = SendMessageRequest(
        message=Message(
            message_id=uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(text='I need to submit a reimbursement request.')],
        )
    )

    task_id = None
    context_id = None
    last_state = None
    async for chunk in client.send_message(first_request):
        print(chunk)
        if chunk.HasField('task'):
            task_id = chunk.task.id
            context_id = chunk.task.context_id
        elif chunk.HasField('status_update'):
            task_id = chunk.status_update.task_id
            context_id = chunk.status_update.context_id
            last_state = chunk.status_update.status.state

    # Second turn: provide the missing details
    if task_id and context_id and last_state == TaskState.TASK_STATE_INPUT_REQUIRED:
        second_request = SendMessageRequest(
            message=Message(
                message_id=uuid4().hex,
                role=Role.ROLE_USER,
                task_id=task_id,
                context_id=context_id,
                parts=[Part(text='Date: 2024-11-15, Amount: $75, Purpose: team offsite dinner.')],
            )
        )
        async for chunk in client.send_message(second_request):
            print(chunk)


if __name__ == '__main__':
    asyncio.run(main())
