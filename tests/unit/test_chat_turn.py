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
    MessageRole,
)
from opsfleet_agent.interactors.chat_turn import ChatTurn
from opsfleet_agent.ports.agent import ProgressSink
from tests.fakes import RecordingProgress

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
    requests: list[AgentRequest] = field(default_factory=list)

    async def respond(self, request: AgentRequest, progress: ProgressSink) -> AgentReply:
        self.requests.append(request)
        return AgentReply(text=self.reply)


def turn(conversations: FakeConversations, guard: FakeGuard, agent: FakeAgent) -> ChatTurn:
    return ChatTurn(
        access=FakeAccess(),
        conversations=conversations,
        guard=guard,
        agent=agent,
        history_max_turns=20,
        history_max_chars=20_000,
    )


async def test_successful_turn_persists_question_and_answer() -> None:
    conversations, agent = FakeConversations(), FakeAgent()

    outcome = await turn(conversations, FakeGuard(), agent)(
        "mgr_a", None, "Q?", RecordingProgress()
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

    first = await chat("mgr_a", None, "first", RecordingProgress())
    await chat("mgr_a", first.session_id, "second", RecordingProgress())

    assert [m.content for m in agent.requests[1].history] == ["first", "Revenue grew 10%."]


async def test_blocked_question_never_reaches_agent_or_history() -> None:
    conversations, agent = FakeConversations(), FakeAgent()

    with pytest.raises(OffTopicError):
        await turn(conversations, FakeGuard(blocked=True), agent)(
            "mgr_a", None, "write python", RecordingProgress()
        )

    assert agent.requests == []
    assert conversations.messages == []


async def test_answer_with_real_email_is_blocked_and_not_persisted() -> None:
    conversations = FakeConversations()
    agent = FakeAgent(reply="Top customer is jane.doe@gmail.com")

    with pytest.raises(PiiDetectedError):
        await turn(conversations, FakeGuard(), agent)("mgr_a", None, "Q?", RecordingProgress())

    assert (MessageRole.ASSISTANT, agent.reply) not in conversations.messages
