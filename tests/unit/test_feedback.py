from dataclasses import dataclass, field
from uuid import UUID

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

from opsfleet_agent.api.schemas import FeedbackRequest
from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.conversation import ChatSession, MessageRole
from opsfleet_agent.entities.feedback import Feedback, Rating
from opsfleet_agent.gateways.observability.feedback import TelemetryFeedbackSink
from opsfleet_agent.interactors.feedback import RecordFeedback
from opsfleet_agent.interactors.pii_redaction import RegexRedactor
from tests.fakes import NOW
from tests.unit.test_chat_turn import FakeConversations

TRACE = "a" * 32


@dataclass
class OwnedConversations(FakeConversations):
    owners: dict[UUID, str] = field(default_factory=dict)

    async def create_session(self, user_id: str, session_id: UUID | None = None) -> ChatSession:
        session = await super().create_session(user_id, session_id)
        self.owners[session.id] = user_id
        return session

    async def get_session(self, session_id: UUID, user_id: str) -> ChatSession:
        if self.owners.get(session_id) != user_id:
            raise NotFoundError(f"session {session_id} not found for user")
        return ChatSession(id=session_id, user_id=user_id)


@dataclass
class FakeFeedbackRepo:
    votes: dict[tuple[str, str], Feedback] = field(default_factory=dict)

    async def upsert(self, feedback: Feedback) -> None:
        self.votes[(feedback.user_id, feedback.trace_id)] = feedback


@dataclass
class RecordingSink:
    published: list[Feedback] = field(default_factory=list)

    async def publish(self, feedback: Feedback) -> None:
        self.published.append(feedback)


@dataclass
class Harness:
    conversations: OwnedConversations
    repo: FakeFeedbackRepo
    sink: RecordingSink
    record: RecordFeedback
    session_id: UUID


@pytest.fixture
async def harness() -> Harness:
    conversations, repo, sink = OwnedConversations(), FakeFeedbackRepo(), RecordingSink()
    session_id = (await conversations.create_session("mgr_a")).id
    await conversations.append(session_id, MessageRole.ASSISTANT, "Revenue grew.", TRACE)
    record = RecordFeedback(
        conversations=conversations,
        feedback=repo,
        sink=sink,
        redactor=RegexRedactor(),
        now=lambda: NOW,
    )
    return Harness(conversations, repo, sink, record, session_id)


async def test_vote_is_stored_and_published(harness: Harness) -> None:
    vote = await harness.record("mgr_a", harness.session_id, TRACE, Rating.UP)

    assert harness.repo.votes == {("mgr_a", TRACE): vote}
    assert harness.sink.published == [vote]
    assert vote.comment is None
    assert vote.created_at == NOW


async def test_later_vote_overwrites_earlier_one(harness: Harness) -> None:
    await harness.record("mgr_a", harness.session_id, TRACE, Rating.UP)
    await harness.record("mgr_a", harness.session_id, TRACE, Rating.DOWN, "wrong quarter")

    [stored] = harness.repo.votes.values()
    assert stored.rating is Rating.DOWN
    assert stored.comment == "wrong quarter"


async def test_cannot_rate_another_users_session(harness: Harness) -> None:
    with pytest.raises(NotFoundError):
        await harness.record("mgr_b", harness.session_id, TRACE, Rating.DOWN)

    assert harness.repo.votes == {}
    assert harness.sink.published == []


async def test_cannot_rate_trace_from_another_session(harness: Harness) -> None:
    other = (await harness.conversations.create_session("mgr_a")).id
    await harness.conversations.append(other, MessageRole.ASSISTANT, "Other.", "b" * 32)

    with pytest.raises(NotFoundError):
        await harness.record("mgr_a", harness.session_id, "b" * 32, Rating.UP)

    assert harness.repo.votes == {}


async def test_comment_pii_is_masked_before_storage(harness: Harness) -> None:
    vote = await harness.record(
        "mgr_a", harness.session_id, TRACE, Rating.DOWN, "email jane.doe@gmail.com about it"
    )

    assert vote.comment is not None
    assert "jane.doe@gmail.com" not in vote.comment
    assert "jane.doe@gmail.com" not in str(harness.sink.published)


async def test_blank_comment_is_dropped(harness: Harness) -> None:
    vote = await harness.record("mgr_a", harness.session_id, TRACE, Rating.DOWN, "   ")

    assert vote.comment is None


def test_rating_maps_to_numeric_score() -> None:
    assert (Rating.UP.score, Rating.DOWN.score) == (1, 0)


@pytest.mark.parametrize(
    "body",
    [
        {"trace_id": TRACE, "rating": "meh"},
        {"trace_id": "", "rating": "up"},
        {"trace_id": TRACE, "rating": "down", "comment": "x" * 501},
        {"trace_id": TRACE, "rating": "up", "user_id": "ceo"},
    ],
)
def test_feedback_request_rejects_invalid_bodies(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FeedbackRequest.model_validate(body)


def upvotes() -> float:
    return REGISTRY.get_sample_value("opsfleet_feedback_total", {"rating": "up"}) or 0.0


async def test_sink_without_langfuse_only_counts(harness: Harness) -> None:
    vote = await harness.record("mgr_a", harness.session_id, TRACE, Rating.UP)
    before = upvotes()

    await TelemetryFeedbackSink(None, salt="test-salt-1234567").publish(vote)

    assert upvotes() == before + 1
