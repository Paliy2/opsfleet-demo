from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.conversation import ChatMessage, ChatSession, MessageRole
from opsfleet_agent.models.tables import ChatMessageRow, ChatSessionRow
from opsfleet_agent.ports.conversations import ConversationRepo


def _message(row: ChatMessageRow) -> ChatMessage:
    return ChatMessage(
        id=row.id, role=MessageRole(row.role), content=row.content, created_at=row.created_at
    )


class SqlConversationRepo(ConversationRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_session(self, user_id: str) -> ChatSession:
        async with self._sessions.begin() as session:
            row = ChatSessionRow(user_id=user_id)
            session.add(row)
            await session.flush()
            return ChatSession(id=row.id, user_id=row.user_id)

    async def get_session(self, session_id: UUID, user_id: str) -> ChatSession:
        async with self._sessions() as session:
            row = await session.get(ChatSessionRow, session_id)
            if row is None or row.user_id != user_id:
                raise NotFoundError(f"session {session_id} not found for user")
            return ChatSession(id=row.id, user_id=row.user_id)

    async def append(
        self, session_id: UUID, role: MessageRole, content: str, trace_id: str | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            session.add(
                ChatMessageRow(
                    session_id=session_id, role=role.value, content=content, trace_id=trace_id
                )
            )

    async def require_trace(self, session_id: UUID, trace_id: str) -> None:
        async with self._sessions() as session:
            found = await session.scalar(
                select(ChatMessageRow.id)
                .where(ChatMessageRow.session_id == session_id)
                .where(ChatMessageRow.trace_id == trace_id)
                .limit(1)
            )
            if found is None:
                raise NotFoundError(f"trace {trace_id} not found in session {session_id}")

    async def recent(
        self, session_id: UUID, max_turns: int, max_chars: int
    ) -> tuple[ChatMessage, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.session_id == session_id)
                .order_by(ChatMessageRow.id.desc())
                .limit(max_turns * 2)
            )
            window: list[ChatMessage] = []
            budget = max_chars
            for row in rows:
                budget -= len(row.content)
                if budget < 0:
                    break
                window.append(_message(row))
            return tuple(reversed(window))
