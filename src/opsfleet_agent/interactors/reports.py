import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from opsfleet_agent.core.errors import (
    AmbiguousCustomerError,
    ConfirmationExpiredError,
    MaskedSearchError,
    NotFoundError,
)
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.pii_tokens import (
    CustomerMatch,
    LookupPurpose,
    MatchedCustomer,
    TokenResolution,
    is_token_term,
    token_label,
)
from opsfleet_agent.entities.privacy import is_masked
from opsfleet_agent.entities.reports import (
    DELETION_BATCH,
    FUZZY_MIN_CHARS,
    REPORT_PAGE_SIZE,
    DeletionFilter,
    DeletionOutcome,
    DeletionPreview,
    DeletionStatus,
    Report,
    ReportFilter,
    ReportPage,
    ReportQuery,
    SearchTerm,
)
from opsfleet_agent.interactors.customer_lookup import ResolveCustomerTokens
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.ports.blobs import BlobStore
from opsfleet_agent.ports.reports import DeletionPlanRepo, ReportRenderer, ReportRepo


class SaveReport:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(
        self, owner_id: str, session_id: UUID, title: str, body: str, chart_paths: Sequence[str]
    ) -> Report:
        ensure_no_pii(f"{title}\n{body}")
        return await self._reports.save(owner_id, session_id, title, body, chart_paths)


@dataclass(frozen=True)
class _ResolvedQuery:
    query: ReportQuery
    corrected: tuple[str, ...] = ()
    customers: tuple[MatchedCustomer, ...] = ()
    impossible: bool = False


@dataclass(frozen=True)
class CustomerSearchTerms:
    include: tuple[SearchTerm | None, ...] = ()
    exclude: tuple[SearchTerm | None, ...] = ()
    customers: tuple[MatchedCustomer, ...] = ()


def _refuse_masked(criteria: ReportFilter) -> None:
    masked = [term for term in criteria.terms if is_masked(term) and not is_token_term(term)]
    if masked:
        raise MaskedSearchError(f"search text holds a privacy placeholder: {masked}")


def _customer_term(customers: tuple[CustomerMatch, ...]) -> SearchTerm | None:
    if not customers:
        return None
    first, *rest = (text for customer in customers for text in customer.report_terms)
    return SearchTerm(text=first, alternatives=tuple(rest))


async def _present_customers(
    reports: ReportRepo, owner_id: str, resolution: TokenResolution
) -> tuple[CustomerMatch, ...]:
    texts = tuple(text for customer in resolution.customers for text in customer.report_terms)
    present = await reports.present_terms(owner_id, texts) if texts else frozenset[str]()
    return tuple(
        customer
        for customer in resolution.customers
        if any(text in present for text in customer.report_terms)
    )


def _token_labels(terms: tuple[str, ...]) -> list[str]:
    return [token_label(term) for term in terms if is_token_term(term)]


class ResolveCustomerTerms:
    def __init__(self, reports: ReportRepo, resolve: ResolveCustomerTokens) -> None:
        self._reports = reports
        self._resolve = resolve

    async def __call__(
        self, scope: AccessScope, session_id: UUID, criteria: ReportFilter, purpose: LookupPurpose
    ) -> CustomerSearchTerms:
        terms: dict[str, SearchTerm | None] = {}
        matched: list[MatchedCustomer] = []
        for label in sorted(set(_token_labels(criteria.terms))):
            resolution = await self._resolve(scope, session_id, label, purpose)
            present = await _present_customers(self._reports, scope.user_id, resolution)
            if purpose is LookupPurpose.DELETE and len(present) > 1:
                names = ", ".join(customer.display for customer in present)
                raise AmbiguousCustomerError(f"reports mention several {label} customers: {names}")
            terms[label] = _customer_term(present)
            matched += [MatchedCustomer(token=label, customer=item) for item in present]
        return CustomerSearchTerms(
            include=tuple(terms[label] for label in _token_labels(criteria.include)),
            exclude=tuple(terms[label] for label in _token_labels(criteria.exclude)),
            customers=tuple(matched),
        )


async def _resolve(
    reports: ReportRepo,
    owner_id: str,
    session_id: UUID,
    criteria: ReportFilter,
    customers: CustomerSearchTerms,
) -> _ResolvedQuery:
    texts = [text.strip() for text in criteria.terms if not is_token_term(text)]
    checked = tuple(dict.fromkeys(text for text in texts if len(text) >= FUZZY_MIN_CHARS))
    found = await reports.present_terms(owner_id, checked) if checked else frozenset[str]()
    corrected: list[str] = []

    def term(text: str) -> SearchTerm:
        exact = SearchTerm(text=text.strip())
        if len(exact.text) < FUZZY_MIN_CHARS or exact.text in found:
            return exact
        corrected.append(exact.text)
        return SearchTerm(text=exact.text, fuzzy=True)

    query = ReportQuery(
        include=(
            *(term(text) for text in criteria.include if not is_token_term(text)),
            *(found_term for found_term in customers.include if found_term is not None),
        ),
        exclude=(
            *(term(text) for text in criteria.exclude if not is_token_term(text)),
            *(found_term for found_term in customers.exclude if found_term is not None),
        ),
        created_after=criteria.created_after,
        created_before=criteria.created_before,
        session_id=session_id if criteria.this_conversation else None,
        report_ids=criteria.report_ids,
    )
    return _ResolvedQuery(
        query, tuple(corrected), customers.customers, impossible=None in customers.include
    )


class SearchReports:
    def __init__(self, reports: ReportRepo, customers: ResolveCustomerTerms) -> None:
        self._reports = reports
        self._customers = customers

    async def __call__(
        self, scope: AccessScope, session_id: UUID, criteria: ReportFilter, page: int
    ) -> ReportPage:
        _refuse_masked(criteria)
        terms = await self._customers(scope, session_id, criteria, LookupPurpose.SEARCH)
        resolved = await _resolve(self._reports, scope.user_id, session_id, criteria, terms)
        return await _page(self._reports, scope.user_id, resolved, page)


async def _page(
    reports: ReportRepo, owner_id: str, resolved: _ResolvedQuery, page: int
) -> ReportPage:
    if page < 1:
        raise ValueError(f"page must be 1 or more, got {page}")
    if resolved.impossible:
        return ReportPage(items=(), total=0, page=page)
    matches = await reports.search(
        owner_id, resolved.query, limit=REPORT_PAGE_SIZE, offset=(page - 1) * REPORT_PAGE_SIZE
    )
    return ReportPage(
        items=matches.items,
        total=matches.total,
        page=page,
        corrected=resolved.corrected,
        customers=resolved.customers,
    )


class ListReports:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(self, owner_id: str, page: int) -> ReportPage:
        return await _page(self._reports, owner_id, _ResolvedQuery(ReportQuery()), page)


class GetReport:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(self, owner_id: str, report_id: UUID) -> Report:
        report = await self._reports.get_owned(owner_id, report_id)
        if report.owner_id != owner_id:
            raise NotFoundError(f"report {report_id} not found for this user")
        return report


class ExportReport:
    def __init__(self, get_report: GetReport, blobs: BlobStore, renderer: ReportRenderer) -> None:
        self._get_report = get_report
        self._blobs = blobs
        self._renderer = renderer

    async def __call__(self, owner_id: str, report_id: UUID) -> tuple[Report, bytes]:
        report = await self._get_report(owner_id, report_id)
        charts: list[bytes] = []
        for path in report.chart_paths:
            try:
                charts.append(await self._blobs.get(path))
            except NotFoundError:
                continue
        missing = len(report.chart_paths) - len(charts)
        return report, await self._renderer.render(report, tuple(charts), missing)


class PlanDeletion:
    def __init__(
        self,
        reports: ReportRepo,
        plans: DeletionPlanRepo,
        customers: ResolveCustomerTerms,
        ttl: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        self._reports = reports
        self._plans = plans
        self._customers = customers
        self._ttl = ttl
        self._now = now

    async def __call__(
        self, thread_id: str, scope: AccessScope, session_id: UUID, criteria: DeletionFilter
    ) -> DeletionPreview:
        _refuse_masked(criteria)
        digest = hashlib.sha256(criteria.model_dump_json().encode()).hexdigest()
        key = f"{thread_id}:{digest}"
        if any(is_token_term(term) for term in criteria.terms):
            planned = await self._plans.find(key, scope.user_id)
            if planned is not None:
                return DeletionPreview.of(planned, len(planned.items))
        terms = await self._customers(scope, session_id, criteria, LookupPurpose.DELETE)
        resolved = await _resolve(self._reports, scope.user_id, session_id, criteria, terms)
        if resolved.impossible:
            raise NotFoundError("no reports you own mention that customer")
        matches = await self._reports.matching(scope.user_id, resolved.query, limit=DELETION_BATCH)
        if not matches:
            raise NotFoundError("no reports you own match that request")
        total = await self._reports.count(scope.user_id, resolved.query)
        plan = await self._plans.get_or_create(key, scope.user_id, matches, self._now() + self._ttl)
        return DeletionPreview.of(plan, max(total, len(plan.items)), resolved.customers)


class ConfirmDeletion:
    def __init__(
        self, reports: ReportRepo, plans: DeletionPlanRepo, now: Callable[[], datetime]
    ) -> None:
        self._reports = reports
        self._plans = plans
        self._now = now

    async def __call__(self, plan_id: UUID, owner_id: str, approved: bool) -> DeletionOutcome:
        plan = await self._plans.get(plan_id, owner_id)
        if plan.status is not DeletionStatus.PENDING or plan.expires_at < self._now():
            raise ConfirmationExpiredError(f"plan {plan_id} is {plan.status} or expired")
        if not approved:
            await self._plans.close(plan.id, DeletionStatus.CANCELLED, deleted=0)
            return DeletionOutcome(deleted=0, cancelled=True)
        deleted = await self._reports.delete_owned(owner_id, [item.id for item in plan.items])
        await self._plans.close(plan.id, DeletionStatus.EXECUTED, deleted=deleted)
        return DeletionOutcome(deleted=deleted, cancelled=False)


class ExpireDeletionPlans:
    def __init__(self, plans: DeletionPlanRepo, now: Callable[[], datetime]) -> None:
        self._plans = plans
        self._now = now

    async def __call__(self) -> int:
        return await self._plans.expire_pending(self._now())
