from datetime import datetime
from uuid import UUID

from sqlalchemy import column, delete, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.conversation import (
    ChatMessage,
    ChatSession,
    MessageRole,
    SessionSummary,
)
from opsfleet_agent.models.tables import ChatMessageRow, ChatSessionRow
from opsfleet_agent.ports.conversations import ConversationRepo

_CHECKPOINT_TABLES = tuple(
    table(name, column("thread_id"))
    for name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
)


def _message(row: ChatMessageRow) -> ChatMessage:
    return ChatMessage(
        id=row.id,
        role=MessageRole(row.role),
        content=row.content,
        created_at=row.created_at,
        trace_id=row.trace_id,
    )


class SqlConversationRepo(ConversationRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_session(self, user_id: str, session_id: UUID | None = None) -> ChatSession:
        try:
            async with self._sessions.begin() as session:
                row = ChatSessionRow(user_id=user_id)
                if session_id is not None:
                    row.id = session_id
                session.add(row)
                await session.flush()
                return ChatSession(id=row.id, user_id=row.user_id)
        except IntegrityError as error:
            raise NotFoundError(f"session id {session_id} is unavailable") from error

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

    async def list_sessions(
        self, user_id: str, limit: int, before: datetime | None = None
    ) -> tuple[SessionSummary, ...]:
        activity = (
            select(
                ChatMessageRow.session_id,
                func.max(ChatMessageRow.created_at).label("updated_at"),
                func.min(ChatMessageRow.id)
                .filter(ChatMessageRow.role == MessageRole.USER.value)
                .label("first_id"),
            )
            .group_by(ChatMessageRow.session_id)
            .subquery()
        )
        query = (
            select(
                ChatSessionRow.id,
                ChatSessionRow.created_at,
                activity.c.updated_at,
                ChatMessageRow.content,
            )
            .join(activity, activity.c.session_id == ChatSessionRow.id)
            .outerjoin(ChatMessageRow, ChatMessageRow.id == activity.c.first_id)
            .where(ChatSessionRow.user_id == user_id)
            .order_by(activity.c.updated_at.desc(), ChatSessionRow.id)
            .limit(limit)
        )
        if before is not None:
            query = query.where(activity.c.updated_at < before)
        async with self._sessions() as session:
            rows = await session.execute(query)
            return tuple(
                SessionSummary(
                    id=row.id,
                    title=row.content or "",
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            )

    async def transcript(self, session_id: UUID) -> tuple[ChatMessage, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.session_id == session_id)
                .order_by(ChatMessageRow.id)
            )
            return tuple(_message(row) for row in rows)

    async def delete_session(self, session_id: UUID) -> None:
        async with self._sessions.begin() as session:
            for checkpoints in _CHECKPOINT_TABLES:
                await session.execute(
                    delete(checkpoints).where(checkpoints.c.thread_id.like(f"{session_id}:%"))
                )
            await session.execute(delete(ChatSessionRow).where(ChatSessionRow.id == session_id))
