from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.pii_tokens import LookupPurpose, TokenEntry
from opsfleet_agent.models.tables import PiiLookupRow
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database
from opsfleet_agent.repos.pii import SqlPiiLookupLog, SqlPiiTokenVault

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Repos:
    sessions: async_sessionmaker[AsyncSession]
    conversations: SqlConversationRepo
    tokens: SqlPiiTokenVault
    lookups: SqlPiiLookupLog


@pytest.fixture
async def repos() -> AsyncIterator[Repos]:
    engine, sessions = create_database(get_settings())
    await SqlAccessRepo(sessions).upsert_user("it_pii", Role.MANAGER, "IT", ["Diesel"])
    yield Repos(
        sessions,
        SqlConversationRepo(sessions),
        SqlPiiTokenVault(sessions),
        SqlPiiLookupLog(sessions),
    )
    await engine.dispose()


def entry(info_type: str, ordinal: int, seed: str) -> TokenEntry:
    return TokenEntry(info_type=info_type, ordinal=ordinal, fingerprint=seed * 64)


async def test_tokens_round_trip_and_a_race_inserts_nothing(repos: Repos) -> None:
    session = await repos.conversations.create_session("it_pii")
    first = [entry("PERSON_NAME", 1, "a"), entry("EMAIL_ADDRESS", 1, "b")]

    assert await repos.tokens.add(session.id, first)
    assert not await repos.tokens.add(session.id, [entry("PERSON_NAME", 2, "c"), first[0]])

    book = await repos.tokens.load(session.id)
    assert {e.label for e in book.entries} == {"PERSON_NAME_1", "EMAIL_ADDRESS_1"}


async def test_a_taken_number_for_a_different_value_is_a_conflict(repos: Repos) -> None:
    session = await repos.conversations.create_session("it_pii")
    await repos.tokens.add(session.id, [entry("PERSON_NAME", 1, "a")])

    assert not await repos.tokens.add(session.id, [entry("PERSON_NAME", 1, "d")])


async def test_tokens_are_deleted_with_the_conversation_and_audit_rows_survive(
    repos: Repos,
) -> None:
    session = await repos.conversations.create_session("it_pii")
    await repos.tokens.add(session.id, [entry("PERSON_NAME", 1, "e")])
    await repos.lookups.record("it_pii", session.id, "PERSON_NAME_1", LookupPurpose.DELETE, 1)

    await repos.conversations.delete_session(session.id)

    assert (await repos.tokens.load(session.id)).entries == ()
    async with repos.sessions() as db:
        audit = (
            await db.scalars(select(PiiLookupRow).where(PiiLookupRow.user_id == "it_pii"))
        ).all()
    assert any(row.session_id is None and row.token == "PERSON_NAME_1" for row in audit)


async def test_lookup_counts_per_conversation_and_per_day(repos: Repos) -> None:
    session = await repos.conversations.create_session("it_pii")
    before = await repos.lookups.count_for_user_since(
        "it_pii", datetime.now(UTC) - timedelta(days=1)
    )

    for _ in range(3):
        await repos.lookups.record("it_pii", session.id, "EMAIL_ADDRESS_1", LookupPurpose.SEARCH, 0)

    assert await repos.lookups.count_in_session(session.id) == 3
    after = await repos.lookups.count_for_user_since(
        "it_pii", datetime.now(UTC) - timedelta(days=1)
    )
    assert after == before + 3
    assert (
        await repos.lookups.count_for_user_since("it_pii", datetime.now(UTC) + timedelta(hours=1))
        == 0
    )
