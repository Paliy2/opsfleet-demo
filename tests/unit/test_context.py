from datetime import UTC, datetime
from uuid import UUID, uuid4

from opsfleet_agent.agents.prompting import describe_context, describe_preferences
from opsfleet_agent.entities.context import (
    AnalysisState,
    AnalysisStep,
    ConversationContext,
    ResultRef,
    UserPreferences,
)
from opsfleet_agent.entities.conversation import (
    AgentReply,
    AgentRequest,
    ChatMessage,
)
from opsfleet_agent.gateways.observability.telemetry import TracingTelemetry
from opsfleet_agent.interactors.chat_turn import ChatTurn
from opsfleet_agent.interactors.context import UpdatePreferences, advance_state
from opsfleet_agent.interactors.pii_redaction import RegexRedactor
from opsfleet_agent.ports.agent import ProgressSink
from tests.fakes import (
    FakeMemory,
    FakePreferences,
    FakeSummarizer,
    RecordingProgress,
    privacy_tokens,
    value_vault,
)
from tests.unit.test_chat_turn import FakeAccess, FakeAgent, FakeConversations, FakeGuard


def step(task: str) -> AnalysisStep:
    return AnalysisStep(
        task=task, results=(ResultRef(id=uuid4(), purpose=task, columns=("brand", "revenue")),)
    )


def test_analysis_state_keeps_most_recent_steps() -> None:
    state = advance_state(AnalysisState(), [step("a"), step("b"), step("c")], max_steps=2)

    assert [s.task for s in state.steps] == ["b", "c"]


async def test_preferences_merge_and_cap_notes() -> None:
    repo = FakePreferences()
    update = UpdatePreferences(repo)
    await update("ceo", UserPreferences(format="table"))
    for index in range(6):
        await update("ceo", UserPreferences(), add_note=f"note {index}")

    updated = await update("ceo", UserPreferences(depth="brief"), remove_note="note 5")

    assert updated.format == "table"
    assert updated.depth == "brief"
    assert updated.notes == ("note 1", "note 2", "note 3", "note 4")
    assert await update.reset("ceo") == UserPreferences()


def test_context_sections_render_state_and_preferences() -> None:
    context = ConversationContext(
        summary="- asked about Q3 revenue",
        state=AnalysisState(steps=(step("Q3 revenue by brand"),)),
        preferences=UserPreferences(format="bullets", notes=("always show YoY",)),
    )

    rendered = describe_context(context)

    assert "asked about Q3 revenue" in rendered
    assert "Q3 revenue by brand" in rendered
    assert str(context.state.steps[0].results[0].id) in rendered
    assert "- format: bullets" in describe_preferences(context.preferences)
    assert describe_preferences(UserPreferences()) == ""


class WindowedConversations(FakeConversations):
    async def recent(
        self, session_id: UUID, max_turns: int, max_chars: int
    ) -> tuple[ChatMessage, ...]:
        return all_messages(self)[-max_turns * 2 :]


def all_messages(conversations: FakeConversations) -> tuple[ChatMessage, ...]:
    return tuple(
        ChatMessage(id=index + 1, role=role, content=content, created_at=datetime.now(UTC))
        for index, (role, content) in enumerate(conversations.messages)
    )


class AnalysisAgent(FakeAgent):
    async def respond(self, request: AgentRequest, progress: ProgressSink) -> AgentReply:
        reply = await super().respond(request, progress)
        return reply.model_copy(update={"analysis": (step(request.question),)})


async def test_turn_summarizes_overflow_and_records_analysis_state() -> None:
    conversations, memory, summarizer = WindowedConversations(), FakeMemory(), FakeSummarizer()
    agent = AnalysisAgent()
    chat = ChatTurn(
        access=FakeAccess(),
        conversations=conversations,
        redactor=RegexRedactor(),
        tokens=privacy_tokens(),
        values=value_vault(),
        guard=FakeGuard(),
        agent=agent,
        telemetry=TracingTelemetry(
            None, salt="test-salt-1234567", environment="test", turn_token_budget=1_000
        ),
        memory=memory,
        preferences=FakePreferences({"mgr_a": UserPreferences(depth="brief")}),
        summarizer=summarizer,
        history_max_turns=1,
        history_max_chars=20_000,
        state_max_steps=5,
    )
    session = uuid4()

    for question in ("q1", "q2", "q3"):
        memory.messages = list(all_messages(conversations))
        await chat("mgr_a", session, question, RecordingProgress())

    assert summarizer.seen == [("", 2)]
    assert memory.summary_text == "+2"
    assert [s.task for s in memory.analysis.steps] == ["q1", "q2", "q3"]
    assert agent.requests[-1].context.preferences.depth == "brief"
    assert [s.task for s in agent.requests[-1].context.state.steps] == ["q1", "q2"]
