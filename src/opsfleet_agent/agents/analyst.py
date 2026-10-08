from dataclasses import dataclass, field
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.agents.prompts import load_prompt
from opsfleet_agent.agents.scope_context import describe_scope
from opsfleet_agent.agents.tool_loop import ToolLoop
from opsfleet_agent.core.errors import (
    AgentError,
    CostLimitError,
    EmptyResultError,
    NotFoundError,
    QueryRejectedError,
    QueryTimeoutError,
    SqlSyntaxError,
)
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.interactors.result_preview import render_preview
from opsfleet_agent.interactors.run_query import RunQuery
from opsfleet_agent.ports.agent import ProgressSink
from opsfleet_agent.ports.results import ResultStore

_RECOVERABLE_QUERY_ERRORS = (SqlSyntaxError, QueryRejectedError, CostLimitError, QueryTimeoutError)


class RunSqlArgs(BaseModel):
    sql: str = Field(description="One BigQuery Standard SQL SELECT using unqualified table names.")
    purpose: str = Field(description="Short plain-English description of what this query answers.")


class GetResultArgs(BaseModel):
    result_id: str = Field(description="result_id returned by run_sql.")
    offset: int = Field(default=0, ge=0, description="Row offset for the next page.")


class AnalystFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str
    result_ids: tuple[UUID, ...]
    llm_calls: int
    sql_failures: int


@dataclass
class _AnalysisRun:
    scope: AccessScope
    session_id: UUID
    progress: ProgressSink
    max_failures: int
    failures: int = 0
    result_ids: list[UUID] = field(default_factory=list)
    result_notes: list[str] = field(default_factory=list)

    @property
    def attempts_left(self) -> int:
        return self.max_failures - self.failures

    def record_failure(self, error: AgentError, label: str) -> str:
        self.failures += 1
        self.progress.emit(ProgressEvent(stage="retry", detail=f"{error.code}: fixing the query"))
        if self.attempts_left <= 0:
            return f"{label} {error.code}: {error.detail}\nSTOP: SQL attempt budget exhausted."
        return f"{label} {error.code}: {error.detail}\n{self.attempts_left} attempts left."


class DataAnalyst:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        run_query: RunQuery,
        results: ResultStore,
        schema_text: str,
        max_steps: int,
        max_sql_failures: int,
        preview_rows: int,
    ) -> None:
        self._model = model
        self._run_query = run_query
        self._results = results
        self._system_prompt = load_prompt("analyst").format(
            schema=schema_text, glossary=load_prompt("glossary")
        )
        self._max_steps = max_steps
        self._max_sql_failures = max_sql_failures
        self._preview_rows = preview_rows

    async def analyze(
        self, task: str, scope: AccessScope, session_id: UUID, progress: ProgressSink
    ) -> AnalystFinding:
        run = _AnalysisRun(scope, session_id, progress, self._max_sql_failures)
        loop = ToolLoop(self._model, self._tools(run), self._max_steps)
        system = f"{self._system_prompt}\n\n{describe_scope(scope)}"
        outcome = await loop.run([SystemMessage(system), HumanMessage(task)])
        references = "\n".join(run.result_notes)
        summary = f"{outcome.text}\n\nStored results:\n{references}" if references else outcome.text
        return AnalystFinding(
            summary=summary,
            result_ids=tuple(run.result_ids),
            llm_calls=outcome.llm_calls,
            sql_failures=run.failures,
        )

    def _tools(self, run: _AnalysisRun) -> list[BaseTool]:
        async def run_sql(sql: str, purpose: str) -> str:
            if run.attempts_left <= 0:
                return "STOP: SQL attempt budget exhausted. Summarize what you established."
            run.progress.emit(ProgressEvent(stage="query", detail=purpose))
            try:
                result = await self._run_query(sql, run.scope)
            except EmptyResultError as error:
                return run.record_failure(error, "NO_ROWS")
            except _RECOVERABLE_QUERY_ERRORS as error:
                return run.record_failure(error, "ERROR")
            result_id = await self._results.save(run.session_id, sql, result)
            run.result_ids.append(result_id)
            run.result_notes.append(
                f"- {result_id}: {purpose} (columns: {', '.join(result.columns)})"
            )
            return render_preview(result_id, result, self._preview_rows)

        async def get_result(result_id: str, offset: int = 0) -> str:
            try:
                stored = await self._results.get(UUID(result_id), run.session_id)
            except (ValueError, NotFoundError):
                return f"ERROR: result_id {result_id} not found in this conversation."
            return render_preview(UUID(result_id), stored.result, self._preview_rows, offset)

        return [
            StructuredTool.from_function(
                coroutine=run_sql,
                name="run_sql",
                description="Execute one read-only BigQuery SELECT and get a preview of rows.",
                args_schema=RunSqlArgs,
            ),
            StructuredTool.from_function(
                coroutine=get_result,
                name="get_result",
                description="Page through more rows of a previous run_sql result.",
                args_schema=GetResultArgs,
            ),
        ]
