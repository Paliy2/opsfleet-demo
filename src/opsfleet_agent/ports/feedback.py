from typing import Protocol

from opsfleet_agent.entities.feedback import Feedback


class FeedbackRepo(Protocol):
    async def upsert(self, feedback: Feedback) -> None:
        """Replaces any earlier vote by the same user on the same trace."""


class FeedbackSink(Protocol):
    async def publish(self, feedback: Feedback) -> None:
        """Forwards a stored vote to observability as a score on its trace."""
