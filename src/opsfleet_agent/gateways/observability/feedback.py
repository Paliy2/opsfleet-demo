from functools import partial

import anyio
from langfuse import Langfuse

from opsfleet_agent.core.identity import user_hash
from opsfleet_agent.entities.feedback import Feedback
from opsfleet_agent.gateways.observability import metrics
from opsfleet_agent.ports.feedback import FeedbackSink

FEEDBACK_SCORE = "user.feedback"


class TelemetryFeedbackSink(FeedbackSink):
    def __init__(self, client: Langfuse | None, *, salt: str) -> None:
        self._client = client
        self._salt = salt

    async def publish(self, feedback: Feedback) -> None:
        metrics.FEEDBACK.labels(feedback.rating.value).inc()
        if self._client is None:
            return
        voter = user_hash(feedback.user_id, self._salt)
        await anyio.to_thread.run_sync(
            partial(
                self._client.create_score,
                name=FEEDBACK_SCORE,
                value=feedback.rating.score,
                data_type="NUMERIC",
                trace_id=feedback.trace_id,
                session_id=str(feedback.session_id),
                score_id=f"feedback-{feedback.trace_id}-{voter}",
                comment=feedback.comment,
                metadata={"user_hash": voter, "rating": feedback.rating.value},
                timestamp=feedback.created_at,
            )
        )
