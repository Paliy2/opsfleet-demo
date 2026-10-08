from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.reports import DeletionStatus
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database
from opsfleet_agent.repos.reports import SqlDeletionPlanRepo, SqlReportRepo

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine, factory = create_database(get_settings())
    access = SqlAccessRepo(factory)
    await access.upsert_user("it_owner", Role.MANAGER, "Owner", ["Diesel"])
    await access.upsert_user("it_other", Role.MANAGER, "Other", ["Diesel"])
    yield factory
    await engine.dispose()


async def test_search_escapes_wildcards_and_is_owner_scoped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reports = SqlReportRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session("it_owner")
    marker = f"Client_{datetime.now(UTC).timestamp()}"
    mine = await reports.save("it_owner", session.id, f"{marker} review", "body", [])
    await reports.save("it_other", session.id, f"{marker} theirs", "body", [])
    await reports.save("it_owner", session.id, "ClientX review", "no marker", [])

    found = await reports.search("it_owner", text_contains=marker)

    assert [item.id for item in found] == [mine.id]


async def test_delete_owned_ignores_other_users_reports(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reports = SqlReportRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session("it_owner")
    theirs = await reports.save("it_other", session.id, "theirs", "body", [])

    deleted = await reports.delete_owned("it_owner", [theirs.id])

    assert deleted == 0
    assert await reports.search("it_other", report_ids=[theirs.id])


async def test_plan_is_idempotent_by_key_and_forgets_titles_when_closed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reports, plans = SqlReportRepo(sessions), SqlDeletionPlanRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session("it_owner")
    saved = await reports.save("it_owner", session.id, "Sensitive Client Y", "body", [])
    items = await reports.search("it_owner", report_ids=[saved.id])
    key = f"it:{saved.id}"
    expires = datetime.now(UTC) + timedelta(minutes=5)

    first = await plans.get_or_create(key, "it_owner", items, expires)
    second = await plans.get_or_create(key, "it_owner", (), expires)
    await plans.close(first.id, DeletionStatus.EXECUTED, deleted=1)
    closed = await plans.get(first.id, "it_owner")

    assert second.id == first.id
    assert second.items == first.items
    assert closed.status is DeletionStatus.EXECUTED
    assert closed.items == ()


async def test_get_owned_returns_own_report_and_hides_others(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reports = SqlReportRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session("it_owner")
    mine = await reports.save("it_owner", session.id, "mine", "my body", [])

    assert (await reports.get_owned("it_owner", mine.id)).body == "my body"
    with pytest.raises(NotFoundError):
        await reports.get_owned("it_other", mine.id)
