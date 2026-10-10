import math
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.core.models import EMBEDDING_DIMENSIONS
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.context import AnalysisState, UserPreferences
from opsfleet_agent.entities.conversation import MessageRole
from opsfleet_agent.models.tables import GoldenTrioRow
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.context import SqlPreferenceRepo, SqlSessionMemory
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database
from opsfleet_agent.repos.golden import SqlGoldenRepo
from tests.fakes import golden_trio
from tests.unit.test_context import step

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine, factory = create_database(get_settings())
    await SqlAccessRepo(factory).upsert_user("it_ctx", Role.CEO, "Context", [])
    yield factory
    async with factory.begin() as session:
        await session.execute(delete(GoldenTrioRow).where(GoldenTrioRow.id.like("it-%")))
    await engine.dispose()


def direction(*weights: float) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    norm = math.sqrt(sum(w * w for w in weights))
    for index, weight in enumerate(weights):
        vector[EMBEDDING_DIMENSIONS - 1 - index] = weight / norm
    return vector


async def test_hybrid_search_ranks_by_similarity_and_drops_irrelevant_text_hits(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlGoldenRepo(sessions)
    await repo.upsert(golden_trio("it-exact"), direction(1, 0))
    await repo.upsert(golden_trio("it-near"), direction(0.9, 0.3))
    lexical_only = golden_trio("it-lexical").model_copy(
        update={"question": "Which warehouses ship slowest?"}
    )
    await repo.upsert(lexical_only, direction(0, 1))

    found = await repo.search("warehouses revenue", direction(1, 0), limit=6, min_similarity=0.8)

    assert [match.trio.id for match in found] == ["it-exact", "it-near"]
    assert found[0].similarity == pytest.approx(1.0)


async def test_upsert_replaces_existing_trio(sessions: async_sessionmaker[AsyncSession]) -> None:
    repo = SqlGoldenRepo(sessions)
    await repo.upsert(golden_trio("it-replace"), direction(1, 0))
    await repo.upsert(golden_trio("it-replace", report="updated"), direction(1, 0))

    [found] = await repo.search("revenue", direction(1, 0), limit=1, min_similarity=0.99)

    assert found.trio.report == "updated"


async def test_preferences_and_session_memory_round_trip(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    preferences = SqlPreferenceRepo(sessions)
    await preferences.save("it_ctx", UserPreferences(format="table", notes=("show YoY",)))
    assert (await preferences.get("it_ctx")).notes == ("show YoY",)

    conversations, memory = SqlConversationRepo(sessions), SqlSessionMemory(sessions)
    session = await conversations.create_session("it_ctx")
    for text in ("q1", "a1", "q2", "a2"):
        await conversations.append(session.id, MessageRole.USER, text)
    window = await conversations.recent(session.id, max_turns=1, max_chars=1_000)

    older = await memory.unsummarized_before(session.id, window[0].id)
    await memory.save_summary(session.id, "- q1 answered", older[-1].id)
    await memory.save_summary(session.id, "- stale", older[0].id)
    await memory.save_state(session.id, AnalysisState(steps=(step("q1"),)))

    assert [m.content for m in older] == ["q1", "a1"]
    assert await memory.unsummarized_before(session.id, window[0].id) == ()
    assert await memory.summary(session.id) == "- q1 answered"
    assert (await memory.state(session.id)).steps[0].task == "q1"
