from dataclasses import dataclass
from uuid import UUID

from opsfleet_agent.entities.conversation import (
    AgentReply,
    AgentRequest,
    MessageRole,
    ProgressEvent,
    TurnOutcome,
)
from opsfleet_agent.entities.telemetry import TurnKind
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.ports.access import AccessRepo
from opsfleet_agent.ports.agent import AnalysisAgent, InputGuard, ProgressSink
from opsfleet_agent.ports.conversations import ConversationRepo
from opsfleet_agent.ports.telemetry import Telemetry, TurnTrace


@dataclass(frozen=True)
class _TracedProgress:
    progress: ProgressSink
    trace: TurnTrace

    def emit(self, event: ProgressEvent) -> None:
        self.trace.emit(event)
        self.progress.emit(event)


class ChatTurn:
    def __init__(
        self,
        *,
        access: AccessRepo,
        conversations: ConversationRepo,
        guard: InputGuard,
        agent: AnalysisAgent,
        telemetry: Telemetry,
        history_max_turns: int,
        history_max_chars: int,
    ) -> None:
        self._access = access
        self._conversations = conversations
        self._guard = guard
        self._agent = agent
        self._telemetry = telemetry
        self._history_max_turns = history_max_turns
        self._history_max_chars = history_max_chars

    async def open_session(self, user_id: str) -> UUID:
        await self._access.get_scope(user_id)
        return (await self._conversations.create_session(user_id)).id

    async def __call__(
        self, user_id: str, session_id: UUID, question: str, progress: ProgressSink
    ) -> TurnOutcome:
        with self._telemetry.turn(TurnKind.QUESTION, user_id, session_id, question) as trace:
            sink = _TracedProgress(progress, trace)
            scope = await self._access.get_scope(user_id)
            session = await self._conversations.get_session(session_id, user_id)
            sink.emit(ProgressEvent(stage="guard"))
            await self._guard.check(question)
            history = await self._conversations.recent(
                session.id, self._history_max_turns, self._history_max_chars
            )
            await self._conversations.append(session.id, MessageRole.USER, question)
            reply = await self._agent.respond(
                AgentRequest(
                    scope=scope, session_id=session.id, question=question, history=history
                ),
                sink,
            )
            return await self._finish(session.id, reply, trace)

    async def confirm(
        self,
        user_id: str,
        session_id: UUID,
        turn_id: UUID,
        approved: bool,
        progress: ProgressSink,
    ) -> TurnOutcome:
        decision = "approved" if approved else "declined"
        with self._telemetry.turn(TurnKind.CONFIRMATION, user_id, session_id, decision) as trace:
            scope = await self._access.get_scope(user_id)
            session = await self._conversations.get_session(session_id, user_id)
            reply = await self._agent.resume(
                scope, session.id, turn_id, approved, _TracedProgress(progress, trace)
            )
            return await self._finish(session.id, reply, trace)

    async def _finish(self, session_id: UUID, reply: AgentReply, trace: TurnTrace) -> TurnOutcome:
        if reply.confirmation is None:
            ensure_no_pii(reply.text)
            await self._conversations.append(session_id, MessageRole.ASSISTANT, reply.text)
        trace.complete(reply)
        return TurnOutcome(session_id=session_id, reply=reply, trace_id=trace.trace_id)
