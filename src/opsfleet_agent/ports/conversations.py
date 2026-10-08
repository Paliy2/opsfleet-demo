from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.conversation import ChatMessage, ChatSession, MessageRole


class ConversationRepo(Protocol):
    async def create_session(self, user_id: str) -> ChatSession: ...

    async def get_session(self, session_id: UUID, user_id: str) -> ChatSession: ...

    async def append(
        self, session_id: UUID, role: MessageRole, content: str, trace_id: str | None = None
    ) -> None: ...

    async def require_trace(self, session_id: UUID, trace_id: str) -> None:
        """Raises NotFoundError unless an answer with this trace id exists in the session."""

    async def recent(
        self, session_id: UUID, max_turns: int, max_chars: int
    ) -> tuple[ChatMessage, ...]: ...
