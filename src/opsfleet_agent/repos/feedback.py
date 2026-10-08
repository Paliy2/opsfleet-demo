from datetime import UTC

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.entities.feedback import Feedback
from opsfleet_agent.models.tables import FeedbackRow
from opsfleet_agent.ports.feedback import FeedbackRepo


class SqlFeedbackRepo(FeedbackRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert(self, feedback: Feedback) -> None:
        values = {
            "session_id": feedback.session_id,
            "rating": feedback.rating.value,
            "comment": feedback.comment,
            "created_at": feedback.created_at.astimezone(UTC).replace(tzinfo=None),
        }
        statement = insert(FeedbackRow).values(
            user_id=feedback.user_id, trace_id=feedback.trace_id, **values
        )
        async with self._sessions.begin() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[FeedbackRow.user_id, FeedbackRow.trace_id], set_=values
                )
            )
