from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.context import AnalysisState, UserPreferences
from opsfleet_agent.entities.conversation import ChatMessage, MessageRole
from opsfleet_agent.models.tables import ChatMessageRow, ChatSessionRow, UserPreferenceRow
from opsfleet_agent.ports.context import PreferenceRepo, SessionMemory


class SqlPreferenceRepo(PreferenceRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, user_id: str) -> UserPreferences:
        async with self._sessions() as session:
            row = await session.get(UserPreferenceRow, user_id)
            return UserPreferences.model_validate(row.data) if row else UserPreferences()

    async def save(self, user_id: str, preferences: UserPreferences) -> None:
        data = preferences.model_dump(mode="json")
        statement = insert(UserPreferenceRow).values(user_id=user_id, data=data)
        async with self._sessions.begin() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[UserPreferenceRow.user_id], set_={"data": data}
                )
            )


class SqlSessionMemory(SessionMemory):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def summary(self, session_id: UUID) -> str:
        return (await self._row(session_id)).summary

    async def unsummarized_before(
        self, session_id: UUID, first_kept_id: int
    ) -> tuple[ChatMessage, ...]:
        row = await self._row(session_id)
        async with self._sessions() as session:
            messages = await session.scalars(
                select(ChatMessageRow)
                .where(
                    ChatMessageRow.session_id == session_id,
                    ChatMessageRow.id > row.summarized_through_id,
                    ChatMessageRow.id < first_kept_id,
                )
                .order_by(ChatMessageRow.id)
            )
            return tuple(
                ChatMessage(
                    id=m.id, role=MessageRole(m.role), content=m.content, created_at=m.created_at
                )
                for m in messages
            )

    async def save_summary(self, session_id: UUID, summary: str, through_id: int) -> None:
        await self._update(session_id, summary=summary, summarized_through_id=through_id)

    async def state(self, session_id: UUID) -> AnalysisState:
        return AnalysisState.model_validate((await self._row(session_id)).analysis_state)

    async def save_state(self, session_id: UUID, state: AnalysisState) -> None:
        await self._update(session_id, analysis_state=state.model_dump(mode="json"))

    async def _row(self, session_id: UUID) -> ChatSessionRow:
        async with self._sessions() as session:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                raise NotFoundError(f"session {session_id} not found")
            return row

    async def _update(self, session_id: UUID, **values: object) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ChatSessionRow).where(ChatSessionRow.id == session_id).values(**values)
            )
