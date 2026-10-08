from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.conversation import MessageRole
from opsfleet_agent.entities.feedback import Feedback, Rating
from opsfleet_agent.models.tables import FeedbackRow
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database
from opsfleet_agent.repos.feedback import SqlFeedbackRepo
from tests.fakes import NOW

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Stores:
    sessions: async_sessionmaker[AsyncSession]
    conversations: SqlConversationRepo
    feedback: SqlFeedbackRepo


@pytest.fixture
async def stores() -> AsyncIterator[Stores]:
    engine, sessions = create_database(get_settings())
    await SqlAccessRepo(sessions).upsert_user("it_voter", Role.MANAGER, "IT", ["Diesel"])
    yield Stores(sessions, SqlConversationRepo(sessions), SqlFeedbackRepo(sessions))
    await engine.dispose()


async def test_answer_trace_is_scoped_to_its_session(stores: Stores) -> None:
    session = await stores.conversations.create_session("it_voter")
    other = await stores.conversations.create_session("it_voter")
    trace = session.id.hex
    await stores.conversations.append(session.id, MessageRole.ASSISTANT, "answer", trace)

    await stores.conversations.require_trace(session.id, trace)
    with pytest.raises(NotFoundError):
        await stores.conversations.require_trace(other.id, trace)


async def test_upsert_keeps_one_vote_per_user_and_trace(stores: Stores) -> None:
    session = await stores.conversations.create_session("it_voter")
    trace = session.id.hex
    vote = Feedback(
        user_id="it_voter", session_id=session.id, trace_id=trace, rating=Rating.UP, created_at=NOW
    )

    await stores.feedback.upsert(vote)
    await stores.feedback.upsert(vote.model_copy(update={"rating": Rating.DOWN, "comment": "no"}))

    async with stores.sessions() as db:
        rows = (await db.scalars(select(FeedbackRow).where(FeedbackRow.trace_id == trace))).all()
    assert [(row.rating, row.comment) for row in rows] == [("down", "no")]
