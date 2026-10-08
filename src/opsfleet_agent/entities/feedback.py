from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

FEEDBACK_COMMENT_MAX_CHARS = 500
TRACE_ID_MAX_CHARS = 64


class Rating(StrEnum):
    UP = "up"
    DOWN = "down"

    @property
    def score(self) -> int:
        return 1 if self is Rating.UP else 0


class Feedback(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    session_id: UUID
    trace_id: str = Field(min_length=1, max_length=TRACE_ID_MAX_CHARS)
    rating: Rating
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)
    created_at: datetime
