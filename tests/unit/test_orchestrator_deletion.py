from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from opsfleet_agent.agents.analyst import DataAnalyst
from opsfleet_agent.agents.orchestrator import Orchestrator, ReportingTools
from opsfleet_agent.agents.report_writer import ReportWriter
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.conversation import AgentRequest
from opsfleet_agent.entities.reports import Chart
from opsfleet_agent.gateways.charts.matplotlib_renderer import MatplotlibChartRenderer
from opsfleet_agent.gateways.prompts.local import LocalPromptSource
from opsfleet_agent.interactors.context import UpdatePreferences
from opsfleet_agent.interactors.reports import (
    ConfirmDeletion,
    GetReport,
    PlanDeletion,
    SaveReport,
    SearchReports,
)
from opsfleet_agent.interactors.run_query import RunQuery
from tests.fakes import (
    ALLOWED,
    NOW,
    FakePlans,
    FakePreferences,
    FakeReports,
    FakeResultStore,
    RecordingProgress,
    ScriptedChatModel,
    ScriptedQueryEngine,
    StoredReport,
    no_examples,
    tool_call,
)


class UnusedCharts:
    async def save(self, session_id: UUID, title: str, png: bytes) -> Chart:
        raise NotImplementedError

    async def get(self, chart_id: UUID, session_id: UUID) -> Chart:
        raise NotImplementedError

    async def read(self, chart_id: UUID, session_id: UUID) -> bytes:
        raise NotImplementedError


SCOPE = AccessScope(user_id="mgr_a", role=Role.MANAGER, brands=frozenset({"Diesel"}))


def orchestrator(model: ScriptedChatModel, reports: FakeReports) -> Orchestrator:
    plans = FakePlans()
    analyst = DataAnalyst(
        model=ScriptedChatModel(script=[]),
        prompts=LocalPromptSource(),
        examples=no_examples(),
        examples_max_chars=10_000,
        run_query=RunQuery(ScriptedQueryEngine(outcomes=[]), ALLOWED, 10_000),
        results=FakeResultStore(),
        schema_text="schema",
        max_steps=3,
        max_sql_failures=1,
        preview_rows=5,
    )
    reporting = ReportingTools(
        writer=ReportWriter(ScriptedChatModel(script=[]), LocalPromptSource()),
        results=FakeResultStore(),
        charts=UnusedCharts(),
        renderer=MatplotlibChartRenderer(),
        save_report=SaveReport(reports),
        search_reports=SearchReports(reports),
        get_report=GetReport(reports),
        plan_deletion=PlanDeletion(reports, plans, ttl=timedelta(minutes=5), now=lambda: NOW),
        confirm_deletion=ConfirmDeletion(reports, plans, now=lambda: NOW),
    )
    return Orchestrator(
        model=model,
        prompts=LocalPromptSource(),
        preferences=UpdatePreferences(FakePreferences()),
        analyst=analyst,
        reporting=reporting,
        schema_text="schema",
        max_steps=5,
        checkpointer=InMemorySaver(),
    )


def request(session_id: UUID) -> AgentRequest:
    return AgentRequest(scope=SCOPE, session_id=session_id, question="delete", history=())


async def test_deletion_pauses_for_confirmation_then_deletes_previewed_reports() -> None:
    session = uuid4()
    reports = FakeReports(
        rows=[
            StoredReport("mgr_a", session, "Client X review", "body"),
            StoredReport("mgr_a", session, "Other", "unrelated"),
        ]
    )
    model = ScriptedChatModel(
        script=[
            tool_call("delete_reports", "d1", include=["Client X"]),
            AIMessage(content="Deleted 1 report."),
        ]
    )
    agent = orchestrator(model, reports)

    paused = await agent.respond(request(session), RecordingProgress())

    assert paused.confirmation is not None
    assert [item.title for item in paused.confirmation.items] == ["Client X review"]
    assert len(reports.rows) == 2

    final = await agent.resume(SCOPE, session, paused.turn_id, True, RecordingProgress())

    assert final.text == "Deleted 1 report."
    assert [row.title for row in reports.rows] == ["Other"]
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert str(tool_results[-1].content).startswith("DELETED: 1")


async def test_declined_deletion_keeps_reports() -> None:
    session = uuid4()
    reports = FakeReports(rows=[StoredReport("mgr_a", session, "Client X review", "body")])
    model = ScriptedChatModel(
        script=[
            tool_call("delete_reports", "d1", this_conversation=True),
            AIMessage(content="Nothing deleted."),
        ]
    )
    agent = orchestrator(model, reports)

    paused = await agent.respond(request(session), RecordingProgress())
    await agent.resume(SCOPE, session, paused.turn_id, False, RecordingProgress())

    assert len(reports.rows) == 1
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert str(tool_results[-1].content).startswith("CANCELLED")


async def test_delete_combined_with_other_tools_is_refused() -> None:
    session = uuid4()
    reports = FakeReports(rows=[StoredReport("mgr_a", session, "Client X review", "body")])
    crowded = AIMessage(
        content="",
        tool_calls=[
            {"name": "search_reports", "args": {}, "id": "l1"},
            {"name": "delete_reports", "args": {"include": ["Client X"]}, "id": "d1"},
        ],
    )
    model = ScriptedChatModel(script=[crowded, AIMessage(content="I must delete separately.")])

    reply = await orchestrator(model, reports).respond(request(session), RecordingProgress())

    assert reply.confirmation is None
    assert len(reports.rows) == 1
    refused = [
        m for m in model.seen[-1] if isinstance(m, ToolMessage) and m.name == "delete_reports"
    ]
    assert "must be called alone" in str(refused[0].content)


async def test_other_users_reports_are_never_offered_for_deletion() -> None:
    session = uuid4()
    reports = FakeReports(rows=[StoredReport("mgr_b", session, "Client X review", "body")])
    model = ScriptedChatModel(
        script=[
            tool_call("delete_reports", "d1", include=["Client X"]),
            AIMessage(content="No matching reports."),
        ]
    )

    reply = await orchestrator(model, reports).respond(request(session), RecordingProgress())

    assert reply.confirmation is None
    assert len(reports.rows) == 1
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert str(tool_results[-1].content).startswith("NO_MATCH")


@pytest.mark.parametrize("tool", ["delete_reports", "search_reports"])
async def test_masked_search_text_is_refused_without_searching(tool: str) -> None:
    session = uuid4()
    reports = FakeReports(rows=[StoredReport("mgr_a", session, "[PERSON_NAME] review", "body")])
    model = ScriptedChatModel(
        script=[
            tool_call(tool, "d1", include=["[PERSON_NAME]"]),
            AIMessage(content="Please retype the brand name."),
        ]
    )

    reply = await orchestrator(model, reports).respond(request(session), RecordingProgress())

    assert reply.confirmation is None
    assert len(reports.rows) == 1
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert str(tool_results[-1].content).startswith("MASKED")


async def test_open_report_returns_own_report_body() -> None:
    session = uuid4()
    mine = StoredReport("mgr_a", session, "Q3 review", "Revenue grew 12%.")
    model = ScriptedChatModel(
        script=[tool_call("open_report", "o1", report_id=str(mine.id)), AIMessage(content="ok")]
    )

    await orchestrator(model, FakeReports(rows=[mine])).respond(
        request(session), RecordingProgress()
    )

    [result] = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert str(result.content).startswith("# Q3 review")
    assert "Revenue grew 12%." in str(result.content)


async def test_open_report_never_reveals_another_users_report() -> None:
    session = uuid4()
    theirs = StoredReport("mgr_b", session, "Secret plan", "confidential body")
    model = ScriptedChatModel(
        script=[tool_call("open_report", "o1", report_id=str(theirs.id)), AIMessage(content="no")]
    )

    await orchestrator(model, FakeReports(rows=[theirs])).respond(
        request(session), RecordingProgress()
    )

    [result] = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert str(result.content).startswith("NOT_FOUND")
    assert "Secret plan" not in str(result.content)
    assert "confidential" not in str(result.content)


async def test_search_tool_returns_one_page_with_a_pointer_to_the_next() -> None:
    session = uuid4()
    reports = FakeReports(
        rows=[StoredReport("mgr_a", session, f"Client X {index}", "body") for index in range(70)]
    )
    model = ScriptedChatModel(
        script=[
            tool_call("search_reports", "s1", include=["client x"]),
            AIMessage(content="You have 70 reports about Client X."),
        ]
    )

    await orchestrator(model, reports).respond(request(session), RecordingProgress())

    [result] = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    lines = str(result.content).splitlines()
    assert lines[0].startswith("70 matching report(s) · page 1 of 3")
    assert sum(line.startswith("- ") for line in lines) == 30
    assert "page=2" in lines[-1]
