from datetime import datetime
from uuid import UUID

from opsfleet_agent.entities.conversation import ChatMessage, SessionSummary
from opsfleet_agent.ports.conversations import ConversationRepo
from opsfleet_agent.ports.pii_token_vault import PiiValueVault

TITLE_MAX_CHARS = 60
UNTITLED = "Untitled conversation"


def headline(question: str) -> str:
    words = " ".join(question.split())
    if len(words) <= TITLE_MAX_CHARS:
        return words or UNTITLED
    return f"{words[: TITLE_MAX_CHARS - 1].rstrip()}…"


class ListConversations:
    def __init__(self, conversations: ConversationRepo) -> None:
        self._conversations = conversations

    async def __call__(
        self, user_id: str, limit: int, before: datetime | None = None
    ) -> tuple[SessionSummary, ...]:
        sessions = await self._conversations.list_sessions(user_id, limit, before)
        return tuple(
            session.model_copy(update={"title": headline(session.title)}) for session in sessions
        )


class GetConversation:
    def __init__(self, conversations: ConversationRepo) -> None:
        self._conversations = conversations

    async def __call__(self, user_id: str, session_id: UUID) -> tuple[ChatMessage, ...]:
        session = await self._conversations.get_session(session_id, user_id)
        return await self._conversations.transcript(session.id)


class DeleteConversation:
    def __init__(self, conversations: ConversationRepo, values: PiiValueVault) -> None:
        self._conversations = conversations
        self._values = values

    async def __call__(self, user_id: str, session_id: UUID) -> None:
        session = await self._conversations.get_session(session_id, user_id)
        await self._values.forget_session(user_id, session.id)
        await self._conversations.delete_session(session.id)
