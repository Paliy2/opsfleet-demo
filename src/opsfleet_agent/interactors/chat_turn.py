from uuid import UUID

from opsfleet_agent.entities.conversation import (
    AgentRequest,
    MessageRole,
    ProgressEvent,
    TurnOutcome,
)
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.ports.access import AccessRepo
from opsfleet_agent.ports.agent import AnalysisAgent, InputGuard, ProgressSink
from opsfleet_agent.ports.conversations import ConversationRepo


class ChatTurn:
    def __init__(
        self,
        *,
        access: AccessRepo,
        conversations: ConversationRepo,
        guard: InputGuard,
        agent: AnalysisAgent,
        history_max_turns: int,
        history_max_chars: int,
    ) -> None:
        self._access = access
        self._conversations = conversations
        self._guard = guard
        self._agent = agent
        self._history_max_turns = history_max_turns
        self._history_max_chars = history_max_chars

    async def __call__(
        self, user_id: str, session_id: UUID | None, question: str, progress: ProgressSink
    ) -> TurnOutcome:
        scope = await self._access.get_scope(user_id)
        session = (
            await self._conversations.get_session(session_id, user_id)
            if session_id
            else await self._conversations.create_session(user_id)
        )
        progress.emit(ProgressEvent(stage="guard"))
        await self._guard.check(question)
        history = await self._conversations.recent(
            session.id, self._history_max_turns, self._history_max_chars
        )
        await self._conversations.append(session.id, MessageRole.USER, question)
        reply = await self._agent.respond(
            AgentRequest(scope=scope, session_id=session.id, question=question, history=history),
            progress,
        )
        ensure_no_pii(reply.text)
        await self._conversations.append(session.id, MessageRole.ASSISTANT, reply.text)
        return TurnOutcome(session_id=session.id, reply=reply)
