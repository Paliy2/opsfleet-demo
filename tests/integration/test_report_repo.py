from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.reports import DeletionStatus, ReportQuery, SearchTerm
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

    found = await reports.matching(
        "it_owner", ReportQuery(include=(SearchTerm(text=marker),)), limit=50
    )
    wildcard = await reports.count("it_owner", ReportQuery(include=(SearchTerm(text="Client%"),)))

    assert [item.id for item in found] == [mine.id]
    assert wildcard == 0


async def test_delete_owned_ignores_other_users_reports(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reports = SqlReportRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session("it_owner")
    theirs = await reports.save("it_other", session.id, "theirs", "body", [])

    deleted = await reports.delete_owned("it_owner", [theirs.id])

    assert deleted == 0
    assert await reports.matching("it_other", ReportQuery(report_ids=(theirs.id,)), limit=50)


async def test_get_returns_body_only_to_owner(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reports = SqlReportRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session("it_owner")
    saved = await reports.save("it_owner", session.id, "Q1 review", "Q1 body", ["chart.png"])

    fetched = await reports.get_owned("it_owner", saved.id)

    assert (fetched.body, fetched.chart_paths) == ("Q1 body", ("chart.png",))
    with pytest.raises(NotFoundError):
        await reports.get_owned("it_other", saved.id)


async def test_plan_is_idempotent_by_key_and_forgets_titles_when_closed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reports, plans = SqlReportRepo(sessions), SqlDeletionPlanRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session("it_owner")
    saved = await reports.save("it_owner", session.id, "Sensitive Client Y", "body", [])
    items = await reports.matching("it_owner", ReportQuery(report_ids=(saved.id,)), limit=50)
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


@pytest.fixture
async def library(sessions: async_sessionmaker[AsyncSession]) -> str:
    owner = f"it_lib_{uuid4().hex[:8]}"
    await SqlAccessRepo(sessions).upsert_user(owner, Role.MANAGER, "Library", ["Diesel"])
    reports = SqlReportRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session(owner)
    await reports.save(owner, session.id, "Q3 review", "Calvin Klein revenue grew 12%", [])
    await reports.save(owner, session.id, "Diesel deep dive", "Diesel returns fell", [])
    await reports.save(owner, session.id, "Churn study", "Customer churn rose in July", [])
    return owner


def terms(*texts: str, fuzzy: bool = False) -> tuple[SearchTerm, ...]:
    return tuple(SearchTerm(text=text, fuzzy=fuzzy) for text in texts)


async def test_misspelled_brand_matches_only_as_a_close_spelling(
    sessions: async_sessionmaker[AsyncSession], library: str
) -> None:
    reports = SqlReportRepo(sessions)

    exact = await reports.count(library, ReportQuery(include=terms("Kalvin Klein")))
    close = await reports.matching(
        library, ReportQuery(include=terms("Kalvin Klein", fuzzy=True)), limit=50
    )

    assert exact == 0
    assert [item.title for item in close] == ["Q3 review"]


async def test_exclusion_and_paging(
    sessions: async_sessionmaker[AsyncSession], library: str
) -> None:
    reports = SqlReportRepo(sessions)
    query = ReportQuery(exclude=terms("Calcin Klein", fuzzy=True))

    first = await reports.search(library, query, limit=1, offset=0)
    second = await reports.search(library, query, limit=1, offset=1)

    assert first.total == 2
    assert {first.items[0].title, second.items[0].title} == {"Diesel deep dive", "Churn study"}


async def test_snippet_shows_the_matching_passage(
    sessions: async_sessionmaker[AsyncSession], library: str
) -> None:
    found = await SqlReportRepo(sessions).search(
        library, ReportQuery(include=terms("churn")), limit=30, offset=0
    )

    assert [hit.snippet for hit in found.items] == ["Customer churn rose in July"]


async def test_date_window_filters_by_saved_time(
    sessions: async_sessionmaker[AsyncSession], library: str
) -> None:
    reports = SqlReportRepo(sessions)
    tomorrow = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)

    assert await reports.count(library, ReportQuery(created_after=tomorrow)) == 0
    assert await reports.count(library, ReportQuery(created_before=tomorrow)) == 3


async def test_present_terms_checks_every_term_in_one_query(
    sessions: async_sessionmaker[AsyncSession], library: str
) -> None:
    present = await SqlReportRepo(sessions).present_terms(
        library, ("calvin klein", "Kalvin Klein", "churn")
    )

    assert present == {"calvin klein", "churn"}


async def test_long_snippet_is_cut_around_the_first_mention(
    sessions: async_sessionmaker[AsyncSession], library: str
) -> None:
    reports = SqlReportRepo(sessions)
    session = await SqlConversationRepo(sessions).create_session(library)
    body = f"{'intro ' * 40}Levi's margin dipped{' outro' * 40}"
    await reports.save(library, session.id, "Long one", body, [])

    found = await reports.search(library, ReportQuery(include=terms("levi's")), limit=30, offset=0)

    [hit] = found.items
    assert hit.snippet.startswith("…")
    assert hit.snippet.endswith("…")
    assert "Levi's margin dipped" in hit.snippet


async def test_matching_stops_at_the_limit(
    sessions: async_sessionmaker[AsyncSession], library: str
) -> None:
    assert len(await SqlReportRepo(sessions).matching(library, ReportQuery(), limit=2)) == 2
