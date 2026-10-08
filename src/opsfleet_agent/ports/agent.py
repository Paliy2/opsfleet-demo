from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.conversation import AgentReply, AgentRequest, ProgressEvent


class ProgressSink(Protocol):
    def emit(self, event: ProgressEvent) -> None: ...


class InputGuard(Protocol):
    async def check(self, text: str) -> None: ...


class AnalysisAgent(Protocol):
    async def respond(self, request: AgentRequest, progress: ProgressSink) -> AgentReply: ...

    async def resume(
        self,
        scope: AccessScope,
        session_id: UUID,
        turn_id: UUID,
        approved: bool,
        progress: ProgressSink,
    ) -> AgentReply: ...
