from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.catalog import StoredResult
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.entities.query import DryRunReport, QueryResult
from opsfleet_agent.entities.reports import (
    DeletionPlan,
    DeletionStatus,
    Report,
    ReportSummary,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
SOURCE = "bigquery-public-data.thelook_ecommerce"
ALLOWED = frozenset(f"{SOURCE}.{t}" for t in ("users", "orders", "order_items", "products"))


@dataclass
class FakeQueryEngine:
    report: DryRunReport = field(
        default_factory=lambda: DryRunReport(
            statement_type="SELECT", referenced_tables=ALLOWED, bytes_processed=1_000
        )
    )
    result: QueryResult = field(
        default_factory=lambda: QueryResult(
            columns=("brand", "revenue"),
            rows=(("Diesel", 100.0),),
            total_rows=1,
            bytes_processed=1_000,
        )
    )
    executed: list[str] = field(default_factory=list)

    async def dry_run(self, sql: str, scope: AccessScope) -> DryRunReport:
        return self.report

    async def execute(self, sql: str, scope: AccessScope) -> QueryResult:
        self.executed.append(sql)
        return self.result


class ScriptedChatModel(BaseChatModel):
    script: list[AIMessage]
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def tool_call(name: str, call_id: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@dataclass
class RecordingProgress:
    events: list[ProgressEvent] = field(default_factory=list)

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)


@dataclass
class FakeResultStore:
    saved: dict[UUID, StoredResult] = field(default_factory=dict)

    async def save(self, session_id: UUID, sql: str, result: QueryResult) -> UUID:
        result_id = uuid4()
        self.saved[result_id] = StoredResult(sql=sql, result=result)
        return result_id

    async def get(self, result_id: UUID, session_id: UUID) -> StoredResult:
        return self.saved[result_id]


@dataclass
class ScriptedQueryEngine:
    outcomes: list[QueryResult | Exception]
    executed: list[str] = field(default_factory=list)

    async def dry_run(self, sql: str, scope: AccessScope) -> DryRunReport:
        return DryRunReport(statement_type="SELECT", referenced_tables=ALLOWED, bytes_processed=1)

    async def execute(self, sql: str, scope: AccessScope) -> QueryResult:
        self.executed.append(sql)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class StoredReport:
    owner_id: str
    session_id: UUID
    title: str
    body: str
    id: UUID = field(default_factory=uuid4)


@dataclass
class FakeReports:
    rows: list[StoredReport] = field(default_factory=list)

    async def save(
        self, owner_id: str, session_id: UUID, title: str, body: str, chart_paths: Sequence[str]
    ) -> Report:
        raise NotImplementedError

    async def search(
        self,
        owner_id: str,
        text_contains: str | None = None,
        session_id: UUID | None = None,
        report_ids: Sequence[UUID] = (),
    ) -> tuple[ReportSummary, ...]:
        return tuple(
            ReportSummary(id=row.id, title=row.title, created_at=NOW)
            for row in self.rows
            if row.owner_id == owner_id
            and (not text_contains or text_contains.lower() in (row.title + row.body).lower())
            and (session_id is None or row.session_id == session_id)
            and (not report_ids or row.id in report_ids)
        )

    async def delete_owned(self, owner_id: str, report_ids: Sequence[UUID]) -> int:
        doomed = [r for r in self.rows if r.owner_id == owner_id and r.id in report_ids]
        self.rows = [r for r in self.rows if r not in doomed]
        return len(doomed)


@dataclass
class FakePlans:
    plans: dict[UUID, DeletionPlan] = field(default_factory=dict)
    keys: dict[str, UUID] = field(default_factory=dict)

    async def get_or_create(
        self, key: str, owner_id: str, items: Sequence[ReportSummary], expires_at: datetime
    ) -> DeletionPlan:
        if key in self.keys:
            return self.plans[self.keys[key]]
        plan = DeletionPlan(
            id=uuid4(),
            owner_id=owner_id,
            items=tuple(items),
            status=DeletionStatus.PENDING,
            expires_at=expires_at,
        )
        self.plans[plan.id] = plan
        self.keys[key] = plan.id
        return plan

    async def get(self, plan_id: UUID, owner_id: str) -> DeletionPlan:
        plan = self.plans.get(plan_id)
        if plan is None or plan.owner_id != owner_id:
            raise NotFoundError("missing")
        return plan

    async def close(self, plan_id: UUID, status: DeletionStatus, deleted: int) -> None:
        self.plans[plan_id] = self.plans[plan_id].model_copy(update={"status": status})
