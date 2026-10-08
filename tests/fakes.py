from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.catalog import StoredResult
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.entities.query import DryRunReport, QueryResult

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
