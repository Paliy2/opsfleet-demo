from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from opsfleet_agent.agents.analyst import DataAnalyst
from opsfleet_agent.core.errors import EmptyResultError, SqlSyntaxError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.query import QueryResult
from opsfleet_agent.gateways.prompts.local import LocalPromptSource
from opsfleet_agent.interactors.run_query import RunQuery
from tests.fakes import (
    ALLOWED,
    FakeResultStore,
    RecordingProgress,
    ScriptedChatModel,
    ScriptedQueryEngine,
    tool_call,
)

SCOPE = AccessScope(user_id="mgr_a", role=Role.MANAGER, brands=frozenset({"Diesel"}))
REVENUE = QueryResult(columns=("revenue",), rows=((42.0,),), total_rows=1, bytes_processed=1)


def analyst(
    model: ScriptedChatModel, engine: ScriptedQueryEngine, max_failures: int = 3
) -> DataAnalyst:
    return DataAnalyst(
        model=model,
        prompts=LocalPromptSource(),
        run_query=RunQuery(engine, allowed_tables=ALLOWED, max_bytes=10_000),
        results=FakeResultStore(),
        schema_text="schema",
        max_steps=6,
        max_sql_failures=max_failures,
        preview_rows=5,
    )


def tool_outputs(model: ScriptedChatModel) -> list[str]:
    return [str(m.content) for m in model.seen[-1] if isinstance(m, ToolMessage)]


async def test_self_corrects_after_sql_syntax_error() -> None:
    model = ScriptedChatModel(
        script=[
            tool_call("run_sql", "c1", sql="SELEC 1", purpose="revenue"),
            tool_call("run_sql", "c2", sql="SELECT 42 AS revenue", purpose="revenue"),
            AIMessage(content="Revenue is 42."),
        ]
    )
    engine = ScriptedQueryEngine(outcomes=[SqlSyntaxError("Syntax error at [1:1]"), REVENUE])
    progress = RecordingProgress()

    finding = await analyst(model, engine).analyze("revenue", SCOPE, uuid4(), progress)

    assert finding.summary.startswith("Revenue is 42.")
    assert "Stored results:" in finding.summary
    assert finding.sql_failures == 1
    assert len(finding.result_ids) == 1
    assert tool_outputs(model)[0].startswith("ERROR E_SQL_SYNTAX")
    assert any(event.stage == "retry" for event in progress.events)


async def test_empty_result_is_reported_as_no_rows() -> None:
    model = ScriptedChatModel(
        script=[
            tool_call("run_sql", "c1", sql="SELECT 1 FROM orders WHERE FALSE", purpose="check"),
            AIMessage(content="No matching data."),
        ]
    )
    engine = ScriptedQueryEngine(outcomes=[EmptyResultError("query returned no rows")])

    await analyst(model, engine).analyze("check", SCOPE, uuid4(), RecordingProgress())

    assert tool_outputs(model)[0].startswith("NO_ROWS E_EMPTY_RESULT")


async def test_failure_budget_stops_further_queries() -> None:
    model = ScriptedChatModel(
        script=[
            tool_call("run_sql", "c1", sql="bad", purpose="try"),
            tool_call("run_sql", "c2", sql="bad again", purpose="try"),
            AIMessage(content="I could not compute it."),
        ]
    )
    engine = ScriptedQueryEngine(outcomes=[SqlSyntaxError("bad")])

    finding = await analyst(model, engine, max_failures=1).analyze(
        "x", SCOPE, uuid4(), RecordingProgress()
    )

    assert len(engine.executed) == 1
    assert tool_outputs(model)[-1].startswith("STOP")
    assert finding.summary == "I could not compute it."


async def test_rejected_qualified_sql_never_reaches_engine() -> None:
    model = ScriptedChatModel(
        script=[
            tool_call(
                "run_sql",
                "c1",
                sql="SELECT email FROM `bigquery-public-data.thelook_ecommerce.users`",
                purpose="emails",
            ),
            AIMessage(content="Not allowed."),
        ]
    )
    engine = ScriptedQueryEngine(outcomes=[])

    await analyst(model, engine).analyze("emails", SCOPE, uuid4(), RecordingProgress())

    assert engine.executed == []
    assert tool_outputs(model)[0].startswith("ERROR E_QUERY_REJECTED")


async def test_scope_is_described_in_system_prompt() -> None:
    model = ScriptedChatModel(script=[AIMessage(content="ok")])

    await analyst(model, ScriptedQueryEngine(outcomes=[])).analyze(
        "x", SCOPE, uuid4(), RecordingProgress()
    )

    assert "Diesel" in str(model.seen[0][0].content)
