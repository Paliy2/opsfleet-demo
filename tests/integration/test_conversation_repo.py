from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.conversation import MessageRole
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database

pytestmark = pytest.mark.integration


@pytest.fixture
async def repo() -> AsyncIterator[SqlConversationRepo]:
    engine, sessions = create_database(get_settings())
    await SqlAccessRepo(sessions).upsert_user("it_user", Role.MANAGER, "IT", ["Diesel"])
    yield SqlConversationRepo(sessions)
    await engine.dispose()


async def test_recent_returns_newest_turns_in_order_within_budget(
    repo: SqlConversationRepo,
) -> None:
    session = await repo.create_session("it_user")
    for index in range(6):
        await repo.append(session.id, MessageRole.USER, f"q{index}-" + "x" * 90)
        await repo.append(session.id, MessageRole.ASSISTANT, f"a{index}-" + "y" * 90)

    window = await repo.recent(session.id, max_turns=20, max_chars=450)

    assert [m.content[:2] for m in window] == ["q4", "a4", "q5", "a5"]


async def test_recent_respects_turn_cap(repo: SqlConversationRepo) -> None:
    session = await repo.create_session("it_user")
    for index in range(5):
        await repo.append(session.id, MessageRole.USER, f"q{index}")
        await repo.append(session.id, MessageRole.ASSISTANT, f"a{index}")

    window = await repo.recent(session.id, max_turns=2, max_chars=10_000)

    assert [m.content for m in window] == ["q3", "a3", "q4", "a4"]


async def test_session_is_private_to_its_owner(repo: SqlConversationRepo) -> None:
    session = await repo.create_session("it_user")

    with pytest.raises(NotFoundError):
        await repo.get_session(session.id, "someone_else")
    with pytest.raises(NotFoundError):
        await repo.get_session(uuid4(), "it_user")


async def test_list_sessions_titles_by_first_question_and_skips_empty(
    repo: SqlConversationRepo,
) -> None:
    empty = await repo.create_session("it_user")
    used = await repo.create_session("it_user")
    await repo.append(used.id, MessageRole.USER, "first question")
    await repo.append(used.id, MessageRole.ASSISTANT, "answer", "c" * 32)
    await repo.append(used.id, MessageRole.USER, "follow up")

    listed = await repo.list_sessions("it_user", limit=100)

    assert empty.id not in {item.id for item in listed}
    assert next(item for item in listed if item.id == used.id).title == "first question"
    assert await repo.list_sessions("someone_else", limit=100) == ()


async def test_transcript_keeps_order_and_trace_ids(repo: SqlConversationRepo) -> None:
    session = await repo.create_session("it_user")
    await repo.append(session.id, MessageRole.USER, "q")
    await repo.append(session.id, MessageRole.ASSISTANT, "a", "d" * 32)

    messages = await repo.transcript(session.id)

    assert [(m.content, m.trace_id) for m in messages] == [("q", None), ("a", "d" * 32)]


async def test_client_chosen_id_is_used_once(repo: SqlConversationRepo) -> None:
    chosen = uuid4()

    created = await repo.create_session("it_user", chosen)

    assert created.id == chosen
    with pytest.raises(NotFoundError):
        await repo.create_session("it_user", chosen)


async def test_delete_session_removes_messages(repo: SqlConversationRepo) -> None:
    session = await repo.create_session("it_user")
    await repo.append(session.id, MessageRole.USER, "q")

    await repo.delete_session(session.id)

    assert await repo.transcript(session.id) == ()
    with pytest.raises(NotFoundError):
        await repo.get_session(session.id, "it_user")
