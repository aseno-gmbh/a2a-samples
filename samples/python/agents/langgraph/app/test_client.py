import asyncio
import logging
from uuid import uuid4

from a2a.client import create_client
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest, TaskState


BASE_URL = 'http://localhost:8080'


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    client = await create_client(BASE_URL)
    logger.info('A2A client initialized.')

    # Simple one-shot message
    request = SendMessageRequest(
        message=Message(
            message_id=uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(text='how much is 10 USD in INR?')],
        )
    )
    async for chunk in client.send_message(request):
        print(chunk)

    # Multi-turn: first turn asks an incomplete question
    first_request = SendMessageRequest(
        message=Message(
            message_id=uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(text='How much is the exchange rate for 1 USD?')],
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

    # Second turn only makes sense if the agent is waiting for more input
    if task_id and context_id and last_state == TaskState.TASK_STATE_INPUT_REQUIRED:
        second_request = SendMessageRequest(
            message=Message(
                message_id=uuid4().hex,
                role=Role.ROLE_USER,
                task_id=task_id,
                context_id=context_id,
                parts=[Part(text='CAD')],
            )
        )
        async for chunk in client.send_message(second_request):
            print(chunk)


if __name__ == '__main__':
    asyncio.run(main())
