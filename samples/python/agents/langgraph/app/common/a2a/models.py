from abc import ABC, abstractmethod

import restate
from pydantic import BaseModel


class A2AAgent(ABC):
    """Minimal agent interface expected by the Restate A2A middleware."""

    @abstractmethod
    async def invoke(
        self,
        ctx: restate.ObjectContext,
        query: str,
        session_id: str,
    ) -> 'AgentInvokeResult':
        pass


class AgentInvokeResult(BaseModel):
    """Normalised result returned from any A2AAgent implementation."""

    parts: list[dict]
    require_user_input: bool = False
    is_task_complete: bool = True
