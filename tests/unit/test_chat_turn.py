from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from opsfleet_agent.core.errors import OffTopicError, PiiDetectedError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.conversation import (
    AgentReply,
    AgentRequest,
    ChatMessage,
    ChatSession,
    ConfirmationRequest,
    MessageRole,
)
from opsfleet_agent.gateways.observability.telemetry import TracingTelemetry
from opsfleet_agent.interactors.chat_turn import ChatTurn
from opsfleet_agent.ports.agent import ProgressSink
from tests.fakes import FakeMemory, FakePreferences, FakeSummarizer, RecordingProgress

TURN = uuid4()
SCOPE = AccessScope(user_id="mgr_a", role=Role.MANAGER, brands=frozenset({"Diesel"}))


@dataclass
class FakeAccess:
    async def get_scope(self, user_id: str) -> AccessScope:
        return SCOPE

    async def upsert_user(
        self, user_id: str, role: Role, display_name: str, brands: Iterable[str]
    ) -> None:
        raise NotImplementedError


@dataclass
class FakeConversations:
    messages: list[tuple[MessageRole, str]] = field(default_factory=list)

    async def create_session(self, user_id: str) -> ChatSession:
        return ChatSession(id=uuid4(), user_id=user_id)

    async def get_session(self, session_id: UUID, user_id: str) -> ChatSession:
        return ChatSession(id=session_id, user_id=user_id)

    async def append(self, session_id: UUID, role: MessageRole, content: str) -> None:
        self.messages.append((role, content))

    async def recent(
        self, session_id: UUID, max_turns: int, max_chars: int
    ) -> tuple[ChatMessage, ...]:
        return tuple(
            ChatMessage(role=role, content=content, created_at=datetime.now(UTC))
            for role, content in self.messages
        )


@dataclass
class FakeGuard:
    blocked: bool = False

    async def check(self, text: str) -> None:
        if self.blocked:
            raise OffTopicError("off topic")


@dataclass
class FakeAgent:
    reply: str = "Revenue grew 10%."
    pending: ConfirmationRequest | None = None
    requests: list[AgentRequest] = field(default_factory=list)
    resumed: list[bool] = field(default_factory=list)

    async def respond(self, request: AgentRequest, progress: ProgressSink) -> AgentReply:
        self.requests.append(request)
        return AgentReply(
            text="" if self.pending else self.reply, turn_id=TURN, confirmation=self.pending
        )

    async def resume(
        self,
        scope: AccessScope,
        session_id: UUID,
        turn_id: UUID,
        approved: bool,
        progress: ProgressSink,
    ) -> AgentReply:
        self.resumed.append(approved)
        return AgentReply(text="DELETED 2 reports.", turn_id=turn_id)


async def chat_session(conversations: FakeConversations) -> UUID:
    return (await conversations.create_session("mgr_a")).id


def turn(
    conversations: FakeConversations,
    guard: FakeGuard,
    agent: FakeAgent,
    memory: FakeMemory | None = None,
    summarizer: FakeSummarizer | None = None,
) -> ChatTurn:
    return ChatTurn(
        access=FakeAccess(),
        conversations=conversations,
        guard=guard,
        agent=agent,
        telemetry=TracingTelemetry(None, salt="test-salt-1234567", environment="test"),
        memory=memory or FakeMemory(),
        preferences=FakePreferences(),
        summarizer=summarizer or FakeSummarizer(),
        history_max_turns=20,
        history_max_chars=20_000,
        state_max_steps=5,
    )


async def test_successful_turn_persists_question_and_answer() -> None:
    conversations, agent = FakeConversations(), FakeAgent()

    outcome = await turn(conversations, FakeGuard(), agent)(
        "mgr_a", await chat_session(conversations), "Q?", RecordingProgress()
    )

    assert outcome.reply.text == "Revenue grew 10%."
    assert conversations.messages == [
        (MessageRole.USER, "Q?"),
        (MessageRole.ASSISTANT, "Revenue grew 10%."),
    ]
    assert agent.requests[0].scope == SCOPE


async def test_history_excludes_current_question() -> None:
    conversations, agent = FakeConversations(), FakeAgent()
    chat = turn(conversations, FakeGuard(), agent)

    first = await chat("mgr_a", await chat_session(conversations), "first", RecordingProgress())
    await chat("mgr_a", first.session_id, "second", RecordingProgress())

    assert [m.content for m in agent.requests[1].history] == ["first", "Revenue grew 10%."]


async def test_blocked_question_never_reaches_agent_or_history() -> None:
    conversations, agent = FakeConversations(), FakeAgent()

    with pytest.raises(OffTopicError) as raised:
        await turn(conversations, FakeGuard(blocked=True), agent)(
            "mgr_a", await chat_session(conversations), "write python", RecordingProgress()
        )

    assert raised.value.trace_id

    assert agent.requests == []
    assert conversations.messages == []


async def test_answer_with_real_email_is_blocked_and_not_persisted() -> None:
    conversations = FakeConversations()
    agent = FakeAgent(reply="Top customer is jane.doe@gmail.com")

    with pytest.raises(PiiDetectedError):
        await turn(conversations, FakeGuard(), agent)(
            "mgr_a", await chat_session(conversations), "Q?", RecordingProgress()
        )

    assert (MessageRole.ASSISTANT, agent.reply) not in conversations.messages


async def test_pending_confirmation_is_not_persisted_until_resumed() -> None:
    conversations = FakeConversations()
    pending = ConfirmationRequest(
        turn_id=TURN, plan_id=uuid4(), items=(), expires_at=datetime.now(UTC)
    )
    agent = FakeAgent(pending=pending)
    chat = turn(conversations, FakeGuard(), agent)

    first = await chat(
        "mgr_a", await chat_session(conversations), "delete my reports", RecordingProgress()
    )
    assert first.reply.confirmation == pending
    assert conversations.messages == [(MessageRole.USER, "delete my reports")]

    final = await chat.confirm("mgr_a", first.session_id, TURN, True, RecordingProgress())

    assert agent.resumed == [True]
    assert final.reply.text == "DELETED 2 reports."
    assert conversations.messages[-1] == (MessageRole.ASSISTANT, "DELETED 2 reports.")


async def test_open_session_gives_id_before_any_turn() -> None:
    conversations = FakeConversations()

    session_id = await turn(conversations, FakeGuard(), FakeAgent()).open_session("mgr_a")

    assert isinstance(session_id, UUID)
