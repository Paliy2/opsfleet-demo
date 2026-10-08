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
