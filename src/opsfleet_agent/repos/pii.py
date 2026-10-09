from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.entities.pii_tokens import LookupPurpose, TokenBook, TokenEntry
from opsfleet_agent.models.tables import PiiLookupRow, PiiTokenRow
from opsfleet_agent.ports.pii_token_vault import PiiLookupLog, PiiTokenVault


class SqlPiiTokenVault(PiiTokenVault):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load(self, session_id: UUID) -> TokenBook:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(PiiTokenRow)
                .where(PiiTokenRow.session_id == session_id)
                .order_by(PiiTokenRow.created_at, PiiTokenRow.info_type, PiiTokenRow.ordinal)
            )
            return TokenBook(
                entries=tuple(
                    TokenEntry(
                        info_type=row.info_type, ordinal=row.ordinal, fingerprint=row.fingerprint
                    )
                    for row in rows
                )
            )

    async def add(self, session_id: UUID, entries: Sequence[TokenEntry]) -> bool:
        statement = (
            insert(PiiTokenRow)
            .values(
                [
                    {
                        "session_id": session_id,
                        "info_type": entry.info_type,
                        "ordinal": entry.ordinal,
                        "fingerprint": entry.fingerprint,
                    }
                    for entry in entries
                ]
            )
            .on_conflict_do_nothing()
            .returning(PiiTokenRow.ordinal)
        )
        async with self._sessions() as session:
            inserted = len((await session.execute(statement)).all())
            if inserted != len(entries):
                await session.rollback()
                return False
            await session.commit()
            return True


class SqlPiiLookupLog(PiiLookupLog):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self, user_id: str, session_id: UUID, label: str, purpose: LookupPurpose, matches: int
    ) -> None:
        async with self._sessions.begin() as session:
            session.add(
                PiiLookupRow(
                    user_id=user_id,
                    session_id=session_id,
                    token=label,
                    purpose=purpose.value,
                    matches=matches,
                )
            )

    async def count_in_session(self, session_id: UUID) -> int:
        async with self._sessions() as session:
            count = await session.scalar(
                select(func.count()).where(PiiLookupRow.session_id == session_id)
            )
            return count or 0

    async def count_for_user_since(self, user_id: str, since: datetime) -> int:
        cutoff = since.astimezone(UTC).replace(tzinfo=None)
        async with self._sessions() as session:
            count = await session.scalar(
                select(func.count()).where(
                    PiiLookupRow.user_id == user_id, PiiLookupRow.created_at >= cutoff
                )
            )
            return count or 0
