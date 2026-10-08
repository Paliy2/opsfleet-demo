from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID, uuid4

import anyio
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError

from opsfleet_agent.agents.analyst import DataAnalyst
from opsfleet_agent.agents.prompting import (
    compose_system,
    describe_context,
    describe_preferences,
)
from opsfleet_agent.agents.report_writer import ReportWriter
from opsfleet_agent.agents.tool_loop import LoopOutcome, ToolLoop
from opsfleet_agent.core.errors import (
    ConfirmationExpiredError,
    MaskedSearchError,
    NotFoundError,
    QueryRejectedError,
)
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.context import AnalysisStep, UserPreferences
from opsfleet_agent.entities.conversation import (
    AgentReply,
    AgentRequest,
    ChatMessage,
    ConfirmationRequest,
    MessageRole,
    ProgressEvent,
)
from opsfleet_agent.entities.prompts import PromptName
from opsfleet_agent.entities.reports import (
    MAX_TERMS,
    REPORT_PAGE_SIZE,
    ChartKind,
    ChartSpec,
    DeletionFilter,
    ReportFilter,
    ReportPage,
)
from opsfleet_agent.interactors.context import UpdatePreferences
from opsfleet_agent.interactors.reports import (
    ConfirmDeletion,
    GetReport,
    PlanDeletion,
    SaveReport,
    SearchReports,
)
from opsfleet_agent.ports.agent import AnalysisAgent, ProgressSink
from opsfleet_agent.ports.prompts import PromptSource
from opsfleet_agent.ports.reports import ChartRenderer, ChartRepo
from opsfleet_agent.ports.results import ResultStore

DELETE_TOOL = "delete_reports"
_MASKED_SEARCH = (
    "MASKED: the search text contains a privacy placeholder such as [PERSON_NAME], so it "
    "cannot match any report. Nothing was searched or deleted. Ask the user to retype the "
    "term, for a brand as it is spelled in the catalogue."
)


class DataAnalystArgs(BaseModel):
    task: str = Field(
        description="Self-contained analysis task: metric, filters, time range, comparison."
    )


class CreateChartArgs(BaseModel):
    result_id: str = Field(description="A stored result id returned by data_analyst.")
    kind: ChartKind
    x: str = Field(description="Result column for categories or time.")
    y: list[str] = Field(min_length=1, max_length=4, description="Numeric result columns.")
    title: str


class CreateReportArgs(BaseModel):
    topic: str = Field(description="What the report covers, including scope and period.")
    findings: str = Field(description="All analyst findings to base the report on, verbatim.")
    chart_ids: list[str] = Field(default_factory=list, description="chart_id values to include.")


class ReportFilterArgs(BaseModel):
    include: list[str] = Field(
        default_factory=list,
        max_length=MAX_TERMS,
        description="Every term must appear in the title or body. A term with no exact match "
        "falls back to close spellings.",
    )
    exclude: list[str] = Field(
        default_factory=list,
        max_length=MAX_TERMS,
        description="Leave out reports that mention any of these terms.",
    )
    created_after: date | None = Field(default=None, description="Saved on or after this day.")
    created_before: date | None = Field(default=None, description="Saved before this day.")
    this_conversation: bool = Field(
        default=False, description="Only reports created in the current conversation."
    )


class SearchReportsArgs(ReportFilterArgs):
    page: int = Field(default=1, ge=1, description=f"Page of {REPORT_PAGE_SIZE} results.")


class OpenReportArgs(BaseModel):
    report_id: str = Field(description="A report id returned by search_reports.")


class DeleteReportsArgs(ReportFilterArgs):
    report_ids: list[str] = Field(default_factory=list, description="Specific report ids.")


class UpdatePreferencesArgs(BaseModel):
    format: Literal["table", "bullets", "prose"] | None = None
    depth: Literal["brief", "standard", "deep"] | None = None
    visuals: Literal["charts", "text"] | None = None
    language: str | None = Field(default=None, description="Answer language, e.g. 'German'.")
    add_note: str | None = Field(
        default=None, max_length=200, description="A standing instruction to remember."
    )
    remove_note: str | None = Field(default=None, description="Exact text of a note to forget.")


class _NoArgs(BaseModel):
    pass


@dataclass(frozen=True)
class ReportingTools:
    writer: ReportWriter
    results: ResultStore
    charts: ChartRepo
    renderer: ChartRenderer
    save_report: SaveReport
    search_reports: SearchReports
    get_report: GetReport
    plan_deletion: PlanDeletion
    confirm_deletion: ConfirmDeletion


@dataclass
class _Turn:
    turn_id: UUID
    scope: AccessScope
    session_id: UUID
    progress: ProgressSink
    preferences: UserPreferences = field(default_factory=UserPreferences)
    llm_calls: int = 0
    result_ids: list[UUID] = field(default_factory=list)
    analysis: list[AnalysisStep] = field(default_factory=list)
    report_ids: list[UUID] = field(default_factory=list)
    chart_paths: list[str] = field(default_factory=list)
    chart_ids: list[UUID] = field(default_factory=list)

    @property
    def thread_id(self) -> str:
        return f"{self.session_id}:{self.turn_id}"


def _as_message(message: ChatMessage) -> AnyMessage:
    if message.role is MessageRole.USER:
        return HumanMessage(message.content)
    return AIMessage(message.content)


def _day(value: date | None) -> datetime | None:
    return None if value is None else datetime.combine(value, time())


def _page_text(page: ReportPage) -> str:
    lines = [
        f"{page.total} matching report(s) · page {page.page} of {max(page.pages, 1)} "
        f"({REPORT_PAGE_SIZE} per page)"
    ]
    if page.corrected:
        lines.append(
            f"No exact match for {', '.join(map(repr, page.corrected))}, so close spellings "
            "were used. Tell the user which spelling matched."
        )
    lines += [
        f"- {hit.id} | {hit.title} | {hit.created_at:%Y-%m-%d %H:%M} | {hit.snippet}"
        for hit in page.items
    ]
    if page.page < page.pages:
        lines.append(
            f"More: call search_reports with the same filters and page={page.page + 1}, "
            "or narrow the filters."
        )
    return "\n".join(lines)


class Orchestrator(AnalysisAgent):
    def __init__(
        self,
        *,
        model: BaseChatModel,
        prompts: PromptSource,
        preferences: UpdatePreferences,
        analyst: DataAnalyst,
        reporting: ReportingTools,
        schema_text: str,
        max_steps: int,
        checkpointer: BaseCheckpointSaver[Any],
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> None:
        self._model = model
        self._prompts = prompts
        self._preferences = preferences
        self._analyst = analyst
        self._reporting = reporting
        self._schema_text = schema_text
        self._max_steps = max_steps
        self._checkpointer = checkpointer
        self._callbacks = callbacks

    async def respond(self, request: AgentRequest, progress: ProgressSink) -> AgentReply:
        turn = _Turn(
            uuid4(), request.scope, request.session_id, progress, request.context.preferences
        )
        progress.emit(ProgressEvent(stage="thinking"))
        instructions = await self._prompts.get(PromptName.ORCHESTRATOR)
        persona = await self._prompts.get(PromptName.PERSONA)
        loop = self._loop(turn, instructions.config.max_steps)
        system = compose_system(
            instructions.text,
            persona.text,
            describe_preferences(request.context.preferences),
            describe_context(request.context),
            scope=request.scope,
        )
        outcome = await loop.run(
            [
                SystemMessage(system),
                *(_as_message(message) for message in request.history),
                HumanMessage(request.question),
            ],
            thread_id=turn.thread_id,
        )
        return await self._reply(turn, outcome)

    async def resume(
        self,
        scope: AccessScope,
        session_id: UUID,
        turn_id: UUID,
        approved: bool,
        progress: ProgressSink,
    ) -> AgentReply:
        turn = _Turn(turn_id, scope, session_id, progress)
        instructions = await self._prompts.get(PromptName.ORCHESTRATOR)
        loop = self._loop(turn, instructions.config.max_steps)
        outcome = await loop.resume(turn.thread_id, {"approved": approved})
        return await self._reply(turn, outcome)

    def _loop(self, turn: _Turn, max_steps: int | None) -> ToolLoop:
        return ToolLoop(
            self._model,
            self._tools(turn),
            max_steps or self._max_steps,
            name="orchestrator",
            exclusive_tools={DELETE_TOOL},
            checkpointer=self._checkpointer,
            callbacks=self._callbacks,
        )

    async def _reply(self, turn: _Turn, outcome: LoopOutcome) -> AgentReply:
        confirmation = (
            ConfirmationRequest.model_validate({"turn_id": turn.turn_id, **outcome.interrupt})
            if outcome.interrupt
            else None
        )
        if confirmation is None:
            await self._checkpointer.adelete_thread(turn.thread_id)
        return AgentReply(
            text=outcome.text,
            turn_id=turn.turn_id,
            result_ids=tuple(turn.result_ids),
            report_ids=tuple(turn.report_ids),
            chart_paths=tuple(turn.chart_paths),
            chart_ids=tuple(turn.chart_ids),
            llm_calls=outcome.llm_calls + turn.llm_calls,
            confirmation=confirmation,
            analysis=tuple(turn.analysis),
        )

    def _tools(self, turn: _Turn) -> list[BaseTool]:
        reporting = self._reporting

        async def data_analyst(task: str) -> str:
            turn.progress.emit(ProgressEvent(stage="analysis", detail=task))
            finding = await self._analyst.analyze(task, turn.scope, turn.session_id, turn.progress)
            turn.llm_calls += finding.llm_calls
            turn.result_ids.extend(finding.result_ids)
            turn.analysis.append(AnalysisStep(task=task, results=finding.results))
            return finding.summary

        async def describe_schema() -> str:
            turn.progress.emit(ProgressEvent(stage="schema"))
            return self._schema_text

        async def create_chart(
            result_id: str, kind: ChartKind, x: str, y: list[str], title: str
        ) -> str:
            turn.progress.emit(ProgressEvent(stage="chart", detail=title))
            spec = ChartSpec(kind=kind, title=title, x=x, y=tuple(y))
            try:
                stored = await reporting.results.get(UUID(result_id), turn.session_id)
                png = await anyio.to_thread.run_sync(reporting.renderer.render, spec, stored.result)
            except (ValueError, NotFoundError, QueryRejectedError) as error:
                return f"ERROR: cannot chart {result_id}: {error}"
            chart = await reporting.charts.save(turn.session_id, title, png)
            turn.chart_paths.append(chart.path)
            turn.chart_ids.append(chart.id)
            return f"chart_id={chart.id} title='{chart.title}'"

        async def create_report(topic: str, findings: str, chart_ids: list[str]) -> str:
            turn.progress.emit(ProgressEvent(stage="report", detail=topic))
            try:
                charts = [
                    await reporting.charts.get(UUID(chart_id), turn.session_id)
                    for chart_id in chart_ids
                ]
            except (ValueError, NotFoundError) as error:
                return f"ERROR: {error}"
            draft = await reporting.writer.write(
                topic, findings, charts, turn.scope, turn.preferences
            )
            report = await reporting.save_report(
                turn.scope.user_id,
                turn.session_id,
                draft.title,
                draft.body,
                [chart.path for chart in charts],
            )
            turn.report_ids.append(report.id)
            return (
                f"Saved report '{report.title}' (id={report.id}). The interface attaches it to "
                "your answer as a downloadable PDF, and the user can reopen it any time from "
                "the Reports library. Do not mention a dashboard.\n\n"
                f"{report.body}"
            )

        async def delete_reports(
            *,
            include: list[str] | None = None,
            exclude: list[str] | None = None,
            created_after: date | None = None,
            created_before: date | None = None,
            this_conversation: bool = False,
            report_ids: list[str] | None = None,
        ) -> str:
            try:
                criteria = DeletionFilter(
                    include=tuple(include or ()),
                    exclude=tuple(exclude or ()),
                    created_after=_day(created_after),
                    created_before=_day(created_before),
                    this_conversation=this_conversation,
                    report_ids=tuple(UUID(value) for value in report_ids or ()),
                )
            except (ValidationError, ValueError) as error:
                return f"ERROR: invalid deletion request: {error}"
            try:
                plan = await reporting.plan_deletion(
                    turn.thread_id, turn.scope.user_id, turn.session_id, criteria
                )
            except MaskedSearchError:
                return _MASKED_SEARCH
            except NotFoundError:
                return "NO_MATCH: none of the user's own reports match. Nothing to delete."
            turn.progress.emit(ProgressEvent(stage="confirm", detail=f"{plan.count} reports"))
            decision = interrupt(plan.model_dump(mode="json"))
            try:
                outcome = await reporting.confirm_deletion(
                    plan.plan_id, turn.scope.user_id, approved=bool(decision.get("approved"))
                )
            except ConfirmationExpiredError:
                return "EXPIRED: the confirmation window passed. Nothing was deleted."
            if outcome.cancelled:
                return "CANCELLED: the user declined. Nothing was deleted."
            left_over = (
                f" {plan.left_over} more still match; offer to delete them next."
                if plan.left_over
                else ""
            )
            return f"DELETED: {outcome.deleted} report(s) permanently deleted.{left_over}"

        async def update_preferences(
            *,
            format: str | None = None,
            depth: str | None = None,
            visuals: str | None = None,
            language: str | None = None,
            add_note: str | None = None,
            remove_note: str | None = None,
        ) -> str:
            requested = {"format": format, "depth": depth, "visuals": visuals, "language": language}
            changes = UserPreferences.model_validate(
                {key: value for key, value in requested.items() if value is not None}
            )
            updated = await self._preferences(turn.scope.user_id, changes, add_note, remove_note)
            turn.preferences = updated
            return f"Saved. Current preferences: {updated.model_dump_json(exclude_defaults=True)}"

        async def reset_preferences() -> str:
            turn.preferences = await self._preferences.reset(turn.scope.user_id)
            return "All preferences cleared."

        return [
            StructuredTool.from_function(
                coroutine=update_preferences,
                name="update_preferences",
                description="Remember how the user wants answers (format, depth, visuals, "
                "language, standing notes). Use when they state a lasting preference.",
                args_schema=UpdatePreferencesArgs,
            ),
            StructuredTool.from_function(
                coroutine=reset_preferences,
                name="reset_preferences",
                description="Forget all of the user's saved preferences.",
                args_schema=_NoArgs,
            ),
            StructuredTool.from_function(
                coroutine=data_analyst,
                name="data_analyst",
                description="Delegate a data question to the SQL analyst; returns findings "
                "and stored result ids.",
                args_schema=DataAnalystArgs,
            ),
            StructuredTool.from_function(
                coroutine=describe_schema,
                name="describe_schema",
                description="Describe available tables, columns and masking rules.",
                args_schema=_NoArgs,
            ),
            StructuredTool.from_function(
                coroutine=create_chart,
                name="create_chart",
                description="Render a chart from a stored analyst result.",
                args_schema=CreateChartArgs,
            ),
            StructuredTool.from_function(
                coroutine=create_report,
                name="create_report",
                description="Write and save an executive report from analyst findings.",
                args_schema=CreateReportArgs,
            ),
            *self._library_tools(turn),
            StructuredTool.from_function(
                coroutine=delete_reports,
                name=DELETE_TOOL,
                description="Request permanent deletion of the user's own reports. The user "
                "confirms in the interface. Call this tool alone.",
                args_schema=DeleteReportsArgs,
            ),
        ]

    def _library_tools(self, turn: _Turn) -> list[BaseTool]:
        reporting = self._reporting

        async def search_reports(
            *,
            include: list[str] | None = None,
            exclude: list[str] | None = None,
            created_after: date | None = None,
            created_before: date | None = None,
            this_conversation: bool = False,
            page: int = 1,
        ) -> str:
            try:
                criteria = ReportFilter(
                    include=tuple(include or ()),
                    exclude=tuple(exclude or ()),
                    created_after=_day(created_after),
                    created_before=_day(created_before),
                    this_conversation=this_conversation,
                )
                found = await reporting.search_reports(
                    turn.scope.user_id, turn.session_id, criteria, page
                )
            except MaskedSearchError:
                return _MASKED_SEARCH
            except (ValidationError, ValueError) as error:
                return f"ERROR: invalid search: {error}"
            return _page_text(found) if found.total else "No saved reports match."

        async def open_report(report_id: str) -> str:
            try:
                report = await reporting.get_report(turn.scope.user_id, UUID(report_id))
            except (ValueError, NotFoundError):
                return f"NOT_FOUND: no saved report of this user has id {report_id}."
            turn.progress.emit(ProgressEvent(stage="library", detail=report.title))
            return f"# {report.title}\nSaved {report.created_at:%Y-%m-%d %H:%M}\n\n{report.body}"

        return [
            StructuredTool.from_function(
                coroutine=search_reports,
                name="search_reports",
                description=f"Search the user's own saved reports, {REPORT_PAGE_SIZE} per page, "
                "newest or best match first. Filter here; never open reports to filter them.",
                args_schema=SearchReportsArgs,
            ),
            StructuredTool.from_function(
                coroutine=open_report,
                name="open_report",
                description="Open one of the user's own saved reports by id and return its "
                "full text.",
                args_schema=OpenReportArgs,
            ),
        ]
