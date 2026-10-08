from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.conversation import AgentReply, ProgressEvent
from opsfleet_agent.entities.telemetry import TraceDetail, TurnKind


class TurnTrace(Protocol):
    @property
    def trace_id(self) -> str: ...

    def emit(self, event: ProgressEvent) -> None: ...

    def complete(self, reply: AgentReply) -> None: ...


class Telemetry(Protocol):
    def turn(
        self, kind: TurnKind, user_id: str, session_id: UUID, question: str
    ) -> AbstractContextManager[TurnTrace]:
        """Records one user-visible turn; tags AgentErrors raised inside with the trace id."""


class TraceReader(Protocol):
    async def get(self, trace_id: str) -> TraceDetail: ...
