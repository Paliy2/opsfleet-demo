from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
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
from opsfleet_agent.entities.context import AnalysisState, UserPreferences
from opsfleet_agent.entities.conversation import (
    ChatMessage,
    ChatSession,
    MessageRole,
    ProgressEvent,
    SessionSummary,
)
from opsfleet_agent.entities.golden import GoldenMatch, GoldenTrio
from opsfleet_agent.entities.pii_tokens import (
    BlindOperator,
    BlindTerm,
    CustomerMatch,
    LookupPurpose,
    TokenBook,
    TokenEntry,
)
from opsfleet_agent.entities.query import DryRunReport, QueryResult
from opsfleet_agent.entities.reports import (
    DeletionPlan,
    DeletionStatus,
    Report,
    ReportHit,
    ReportMatches,
    ReportQuery,
    ReportSummary,
    SearchTerm,
)
from opsfleet_agent.gateways.pii_vault.sealer import ValueSealer
from opsfleet_agent.gateways.pii_vault.vaults import InProcessHashStore, SealedPiiValueVault
from opsfleet_agent.interactors.customer_lookup import LookupPolicy, ResolveCustomerTokens
from opsfleet_agent.interactors.golden import RetrieveExamples
from opsfleet_agent.interactors.pii_tokens import PrivacyTokens
from opsfleet_agent.interactors.reports import ResolveCustomerTerms
from opsfleet_agent.ports.pii_token_vault import PiiValueVault
from opsfleet_agent.ports.reports import ReportRepo

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
    chart_paths: tuple[str, ...] = ()
    created_at: datetime = NOW


_CLOSE_SPELLING = 0.75


def _mentions(row: StoredReport, term: SearchTerm) -> bool:
    text = f"{row.title} {row.body}".lower()
    if not term.fuzzy:
        return any(option.lower() in text for option in term.texts)
    words = text.split()
    size = len(term.text.split())
    windows = (" ".join(words[start : start + size]) for start in range(len(words)))
    return any(
        SequenceMatcher(None, term.text.lower(), window).ratio() >= 0.75 for window in windows
    )


def _report_matches(row: StoredReport, owner_id: str, query: ReportQuery) -> bool:
    return (
        row.owner_id == owner_id
        and all(_mentions(row, term) for term in query.include)
        and not any(_mentions(row, term) for term in query.exclude)
        and (query.created_after is None or row.created_at >= query.created_after)
        and (query.created_before is None or row.created_at < query.created_before)
        and (query.session_id is None or row.session_id == query.session_id)
        and (not query.report_ids or row.id in query.report_ids)
    )


@dataclass
class FakeReports:
    rows: list[StoredReport] = field(default_factory=list)
    term_checks: int = 0

    async def save(
        self, owner_id: str, session_id: UUID, title: str, body: str, chart_paths: Sequence[str]
    ) -> Report:
        raise NotImplementedError

    async def search(
        self, owner_id: str, query: ReportQuery, *, limit: int, offset: int
    ) -> ReportMatches:
        found = [row for row in self.rows if _report_matches(row, owner_id, query)]
        return ReportMatches(
            items=tuple(
                ReportHit(id=row.id, title=row.title, created_at=row.created_at, snippet=row.body)
                for row in found[offset : offset + limit]
            ),
            total=len(found),
        )

    async def matching(
        self, owner_id: str, query: ReportQuery, *, limit: int
    ) -> tuple[ReportSummary, ...]:
        found = [row for row in self.rows if _report_matches(row, owner_id, query)]
        return tuple(
            ReportSummary(id=row.id, title=row.title, created_at=row.created_at)
            for row in found[:limit]
        )

    async def count(self, owner_id: str, query: ReportQuery) -> int:
        return sum(_report_matches(row, owner_id, query) for row in self.rows)

    async def present_terms(self, owner_id: str, texts: Sequence[str]) -> frozenset[str]:
        self.term_checks += 1
        return frozenset(
            text
            for text in texts
            if any(
                row.owner_id == owner_id and text.lower() in f"{row.title} {row.body}".lower()
                for row in self.rows
            )
        )

    async def get_owned(self, owner_id: str, report_id: UUID) -> Report:
        for row in self.rows:
            if row.id == report_id and row.owner_id == owner_id:
                return Report(
                    id=row.id,
                    owner_id=row.owner_id,
                    session_id=row.session_id,
                    title=row.title,
                    body=row.body,
                    chart_paths=row.chart_paths,
                    created_at=NOW,
                )
        raise NotFoundError(f"report {report_id} not found")

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

    async def find(self, key: str, owner_id: str) -> DeletionPlan | None:
        plan_id = self.keys.get(key)
        plan = None if plan_id is None else self.plans[plan_id]
        return plan if plan is not None and plan.owner_id == owner_id else None

    async def get(self, plan_id: UUID, owner_id: str) -> DeletionPlan:
        plan = self.plans.get(plan_id)
        if plan is None or plan.owner_id != owner_id:
            raise NotFoundError("missing")
        return plan

    async def close(self, plan_id: UUID, status: DeletionStatus, deleted: int) -> None:
        self.plans[plan_id] = self.plans[plan_id].model_copy(update={"status": status})

    async def expire_pending(self, now: datetime) -> int:
        stale = [
            plan.id
            for plan in self.plans.values()
            if plan.status is DeletionStatus.PENDING and plan.expires_at < now
        ]
        for plan_id in stale:
            await self.close(plan_id, DeletionStatus.EXPIRED, 0)
        return len(stale)


def golden_trio(
    trio_id: str = "revenue-trends--q", report: str = "Revenue was $123,456."
) -> GoldenTrio:
    return GoldenTrio(
        id=trio_id,
        category="revenue-trends",
        question="What was revenue last quarter?",
        approach="Sum order_items.sale_price excluding Cancelled and Returned.",
        sql="SELECT ROUND(SUM(sale_price), 2) AS revenue FROM order_items",
        report=report,
    )


@dataclass
class FakeEmbedder:
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]


@dataclass
class FakeGoldenRepo:
    matches: tuple[GoldenMatch, ...] = ()
    upserted: dict[str, GoldenTrio] = field(default_factory=dict)
    searches: list[tuple[str, int, float]] = field(default_factory=list)

    async def upsert(self, trio: GoldenTrio, embedding: Sequence[float]) -> None:
        self.upserted[trio.id] = trio

    async def search(
        self, query: str, embedding: Sequence[float], limit: int, min_similarity: float
    ) -> tuple[GoldenMatch, ...]:
        self.searches.append((query, limit, min_similarity))
        return self.matches[:limit]


def no_examples() -> RetrieveExamples:
    return RetrieveExamples(FakeEmbedder(), FakeGoldenRepo(), limit=6, min_similarity=0.6)


@dataclass
class FakePreferences:
    saved: dict[str, UserPreferences] = field(default_factory=dict)

    async def get(self, user_id: str) -> UserPreferences:
        return self.saved.get(user_id, UserPreferences())

    async def save(self, user_id: str, preferences: UserPreferences) -> None:
        self.saved[user_id] = preferences


@dataclass
class FakeMemory:
    messages: list[ChatMessage] = field(default_factory=list)
    summary_text: str = ""
    summarized_through: int = 0
    analysis: AnalysisState = field(default_factory=AnalysisState)

    async def summary(self, session_id: UUID) -> str:
        return self.summary_text

    async def unsummarized_before(
        self, session_id: UUID, first_kept_id: int
    ) -> tuple[ChatMessage, ...]:
        return tuple(m for m in self.messages if self.summarized_through < m.id < first_kept_id)

    async def save_summary(self, session_id: UUID, summary: str, through_id: int) -> None:
        self.summary_text, self.summarized_through = summary, through_id

    async def state(self, session_id: UUID) -> AnalysisState:
        return self.analysis

    async def save_state(self, session_id: UUID, state: AnalysisState) -> None:
        self.analysis = state


@dataclass
class FakeSummarizer:
    seen: list[tuple[str, int]] = field(default_factory=list)

    async def summarize(self, previous: str, messages: Sequence[ChatMessage]) -> str:
        self.seen.append((previous, len(messages)))
        return f"{previous}+{len(messages)}"


@dataclass
class MemoryConversations:
    owners: dict[UUID, str] = field(default_factory=dict)
    messages: dict[UUID, list[ChatMessage]] = field(default_factory=dict)
    deleted: list[UUID] = field(default_factory=list)

    async def create_session(self, user_id: str, session_id: UUID | None = None) -> ChatSession:
        new_id = session_id or uuid4()
        if new_id in self.owners:
            raise NotFoundError(f"session id {new_id} is unavailable")
        self.owners[new_id] = user_id
        self.messages[new_id] = []
        return ChatSession(id=new_id, user_id=user_id)

    async def get_session(self, session_id: UUID, user_id: str) -> ChatSession:
        if self.owners.get(session_id) != user_id:
            raise NotFoundError(f"session {session_id} not found for user")
        return ChatSession(id=session_id, user_id=user_id)

    async def append(
        self, session_id: UUID, role: MessageRole, content: str, trace_id: str | None = None
    ) -> None:
        log = self.messages[session_id]
        log.append(
            ChatMessage(
                id=len(log) + 1, role=role, content=content, created_at=NOW, trace_id=trace_id
            )
        )

    async def require_trace(self, session_id: UUID, trace_id: str) -> None:
        if not any(message.trace_id == trace_id for message in self.messages[session_id]):
            raise NotFoundError(f"trace {trace_id} not in session")

    async def recent(
        self, session_id: UUID, max_turns: int, max_chars: int
    ) -> tuple[ChatMessage, ...]:
        return tuple(self.messages[session_id][-max_turns * 2 :])

    async def list_sessions(
        self, user_id: str, limit: int, before: datetime | None = None
    ) -> tuple[SessionSummary, ...]:
        return tuple(
            SessionSummary(
                id=session_id,
                title=next(m.content for m in log if m.role is MessageRole.USER),
                created_at=NOW,
                updated_at=NOW,
            )
            for session_id, log in self.messages.items()
            if log and self.owners[session_id] == user_id
        )[:limit]

    async def transcript(self, session_id: UUID) -> tuple[ChatMessage, ...]:
        return tuple(self.messages[session_id])

    async def delete_session(self, session_id: UUID) -> None:
        self.deleted.append(session_id)
        del self.owners[session_id]
        del self.messages[session_id]


@dataclass
class MemoryPiiTokenVault:
    books: dict[UUID, TokenBook] = field(default_factory=dict)
    conflicts: int = 0

    async def load(self, session_id: UUID) -> TokenBook:
        return self.books.get(session_id, TokenBook())

    async def add(self, session_id: UUID, entries: Sequence[TokenEntry]) -> bool:
        book = await self.load(session_id)
        taken = {(e.info_type, e.ordinal) for e in book.entries} | set(book.by_fingerprint())
        if self.conflicts or any(
            (e.info_type, e.ordinal) in taken or e.fingerprint in taken for e in entries
        ):
            self.conflicts = max(self.conflicts - 1, 0)
            return False
        for entry in entries:
            book = book.with_entry(entry)
        self.books[session_id] = book
        return True


@dataclass
class FakeCustomerDirectory:
    customers: dict[str, tuple[CustomerMatch, ...]] = field(default_factory=dict)
    terms: list[BlindTerm] = field(default_factory=list)

    async def find(
        self, term: BlindTerm, scope: AccessScope, limit: int
    ) -> tuple[CustomerMatch, ...]:
        self.terms.append(term)
        return self.customers.get(term.values[0].get_secret_value(), ())[:limit]


@dataclass
class MemoryLookupLog:
    rows: list[tuple[str, UUID, str, LookupPurpose, int, datetime]] = field(default_factory=list)
    now: datetime = NOW

    async def record(
        self, user_id: str, session_id: UUID, label: str, purpose: LookupPurpose, matches: int
    ) -> None:
        self.rows.append((user_id, session_id, label, purpose, matches, self.now))

    async def count_in_session(self, session_id: UUID) -> int:
        return sum(1 for row in self.rows if row[1] == session_id)

    async def count_for_user_since(self, user_id: str, since: datetime) -> int:
        return sum(1 for row in self.rows if row[0] == user_id and row[5] >= since)


TEST_PII_KEY = bytes(range(32))


def value_vault(store: InProcessHashStore | None = None) -> SealedPiiValueVault:
    return SealedPiiValueVault(
        store or InProcessHashStore(lambda: NOW),
        ValueSealer(TEST_PII_KEY),
        owner_salt="test-salt-1234567",
        ttl=timedelta(minutes=15),
    )


def privacy_tokens(vault: MemoryPiiTokenVault | None = None) -> PrivacyTokens:
    return PrivacyTokens(vault or MemoryPiiTokenVault(), b"test-numbering-key")


def customer(
    hex_id: str, first: str = "Morgan", last: str = "Costa", kind: str = "exact"
) -> CustomerMatch:
    return CustomerMatch(
        id=hex_id,
        first_name=first,
        last_name=last,
        email=f"user_{hex_id[:8]}@example.com",
        match_kind=BlindOperator(kind),
    )


def no_customers(reports: ReportRepo) -> ResolveCustomerTerms:
    return ResolveCustomerTerms(reports, customer_lookup(value_vault()))


def customer_lookup(
    values: PiiValueVault,
    directory: FakeCustomerDirectory | None = None,
    log: MemoryLookupLog | None = None,
    policy: LookupPolicy | None = None,
) -> ResolveCustomerTokens:
    return ResolveCustomerTokens(
        directory or FakeCustomerDirectory(),
        log or MemoryLookupLog(),
        values,
        policy or LookupPolicy(),
        now=lambda: NOW,
    )
