from datetime import datetime
from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.conversation import (
    ChatMessage,
    ChatSession,
    MessageRole,
    SessionSummary,
)


class ConversationRepo(Protocol):
    async def create_session(self, user_id: str, session_id: UUID | None = None) -> ChatSession:
        """Raises NotFoundError when session_id is already taken by another session."""

    async def get_session(self, session_id: UUID, user_id: str) -> ChatSession: ...

    async def append(
        self, session_id: UUID, role: MessageRole, content: str, trace_id: str | None = None
    ) -> None: ...

    async def require_trace(self, session_id: UUID, trace_id: str) -> None:
        """Raises NotFoundError unless an answer with this trace id exists in the session."""

    async def recent(
        self, session_id: UUID, max_turns: int, max_chars: int
    ) -> tuple[ChatMessage, ...]: ...

    async def list_sessions(
        self, user_id: str, limit: int, before: datetime | None = None
    ) -> tuple[SessionSummary, ...]:
        """Sessions with at least one message, most recently active first."""

    async def transcript(self, session_id: UUID) -> tuple[ChatMessage, ...]: ...

    async def delete_session(self, session_id: UUID) -> None:
        """Removes the session, its messages, results, charts and agent checkpoints."""
