from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from opsfleet_agent.entities.feedback import Feedback, Rating
from opsfleet_agent.ports.conversations import ConversationRepo
from opsfleet_agent.ports.feedback import FeedbackRepo, FeedbackSink
from opsfleet_agent.ports.privacy import PiiRedactor


class RecordFeedback:
    def __init__(
        self,
        *,
        conversations: ConversationRepo,
        feedback: FeedbackRepo,
        sink: FeedbackSink,
        redactor: PiiRedactor,
        now: Callable[[], datetime],
    ) -> None:
        self._conversations = conversations
        self._feedback = feedback
        self._sink = sink
        self._redactor = redactor
        self._now = now

    async def __call__(
        self,
        user_id: str,
        session_id: UUID,
        trace_id: str,
        rating: Rating,
        comment: str | None = None,
    ) -> Feedback:
        session = await self._conversations.get_session(session_id, user_id)
        await self._conversations.require_trace(session.id, trace_id)
        vote = Feedback(
            user_id=user_id,
            session_id=session.id,
            trace_id=trace_id,
            rating=rating,
            comment=await self._masked(comment),
            created_at=self._now(),
        )
        await self._feedback.upsert(vote)
        await self._sink.publish(vote)
        return vote

    async def _masked(self, comment: str | None) -> str | None:
        stripped = (comment or "").strip()
        if not stripped:
            return None
        return (await self._redactor.redact(stripped)).text
