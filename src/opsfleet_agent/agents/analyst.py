import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.agents.prompting import compose_system, render
from opsfleet_agent.agents.tool_loop import ToolLoop
from opsfleet_agent.core.errors import (
    AgentError,
    EmptyResultError,
    MaskedValueError,
    NotFoundError,
)
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.context import ResultRef
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.entities.golden import GoldenMatch
from opsfleet_agent.entities.privacy import placeholders_in
from opsfleet_agent.entities.prompts import PromptName
from opsfleet_agent.interactors.golden import RetrieveExamples, render_examples
from opsfleet_agent.interactors.result_preview import render_preview
from opsfleet_agent.interactors.run_query import RECOVERABLE_QUERY_ERRORS, RunQuery
from opsfleet_agent.ports.agent import ProgressSink
from opsfleet_agent.ports.prompts import PromptSource
from opsfleet_agent.ports.results import ResultStore

_EXAMPLES_HEADER = (
    "## Validated examples\n"
    "Similar questions answered correctly before, with the SQL that ran. Reuse their "
    "definitions and join patterns; adapt filters, periods and grouping to the current task."
)


class RunSqlArgs(BaseModel):
    sql: str = Field(description="One BigQuery Standard SQL SELECT using unqualified table names.")
    purpose: str = Field(description="Short plain-English description of what this query answers.")


class GetResultArgs(BaseModel):
    result_id: str = Field(description="result_id returned by run_sql.")
    offset: int = Field(default=0, ge=0, description="Row offset for the next page.")


class AnalystFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str
    results: tuple[ResultRef, ...]
    llm_calls: int
    sql_failures: int
    example_ids: tuple[str, ...] = ()

    @property
    def result_ids(self) -> tuple[UUID, ...]:
        return tuple(result.id for result in self.results)


@dataclass
class _AnalysisRun:
    scope: AccessScope
    session_id: UUID
    progress: ProgressSink
    max_failures: int
    failures: int = 0
    results: list[ResultRef] = field(default_factory=list)

    @property
    def attempts_left(self) -> int:
        return self.max_failures - self.failures

    def record_failure(self, error: AgentError, label: str) -> str:
        self.failures += 1
        self.progress.emit(
            ProgressEvent(stage="retry", detail=f"{error.code}: fixing the query", code=error.code)
        )
        if self.attempts_left <= 0:
            return f"{label} {error.code}: {error.detail}\nSTOP: SQL attempt budget exhausted."
        return f"{label} {error.code}: {error.detail}\n{self.attempts_left} attempts left."


def _examples_section(matches: Sequence[GoldenMatch], max_chars: int) -> str:
    if not matches:
        return ""
    return f"{_EXAMPLES_HEADER}\n\n{render_examples(matches, max_chars)}"


def _with_references(text: str, results: Sequence[ResultRef]) -> str:
    if not results:
        return text
    lines = "\n".join(
        f"- {result.id}: {result.purpose} (columns: {', '.join(result.columns)})"
        for result in results
    )
    return f"{text}\n\nStored results:\n{lines}"


class DataAnalyst:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        prompts: PromptSource,
        examples: RetrieveExamples,
        examples_max_chars: int,
        run_query: RunQuery,
        results: ResultStore,
        schema_text: str,
        max_steps: int,
        max_sql_failures: int,
        preview_rows: int,
    ) -> None:
        self._model = model
        self._prompts = prompts
        self._examples = examples
        self._examples_max_chars = examples_max_chars
        self._run_query = run_query
        self._results = results
        self._schema_text = schema_text
        self._max_steps = max_steps
        self._max_sql_failures = max_sql_failures
        self._preview_rows = preview_rows

    async def analyze(
        self, task: str, scope: AccessScope, session_id: UUID, progress: ProgressSink
    ) -> AnalystFinding:
        hidden = placeholders_in(task)
        if hidden:
            raise MaskedValueError(f"analysis task holds a privacy placeholder: {list(hidden)}")
        prompt, glossary, matches = await asyncio.gather(
            self._prompts.get(PromptName.ANALYST),
            self._prompts.get(PromptName.GLOSSARY),
            self._examples(task),
        )
        if matches:
            progress.emit(
                ProgressEvent(
                    stage="examples", detail=", ".join(match.trio.id for match in matches)
                )
            )
        limits = prompt.config
        run = _AnalysisRun(
            scope, session_id, progress, limits.max_sql_failures or self._max_sql_failures
        )
        loop = ToolLoop(
            self._model, self._tools(run), limits.max_steps or self._max_steps, name="analyst"
        )
        system = compose_system(
            render(prompt.text, schema=self._schema_text, glossary=glossary.text),
            _examples_section(matches, self._examples_max_chars),
            scope=scope,
        )
        outcome = await loop.run([SystemMessage(system), HumanMessage(task)])
        return AnalystFinding(
            summary=_with_references(outcome.text, run.results),
            results=tuple(run.results),
            llm_calls=outcome.llm_calls,
            sql_failures=run.failures,
            example_ids=tuple(match.trio.id for match in matches),
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
            except RECOVERABLE_QUERY_ERRORS as error:
                return run.record_failure(error, "ERROR")
            result_id = await self._results.save(run.session_id, sql, result)
            run.results.append(ResultRef(id=result_id, purpose=purpose, columns=result.columns))
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
