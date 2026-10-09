import re
from collections import defaultdict
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict

from evals.pii.cases import BRAND_KINDS, InfoType, PiiCase, Preserved
from evals.scoring import middle, percentile
from opsfleet_agent.entities.privacy import Redaction


class Leak(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    info_type: InfoType
    fragments: tuple[str, ...]


class CaseOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    category: str
    contains_pii: bool
    text: str
    redacted: str
    findings: tuple[str, ...]
    leaked: tuple[Leak, ...]
    masked: tuple[Preserved, ...]
    removed_types: tuple[InfoType, ...]
    passed: bool
    seconds: float

    @property
    def detected(self) -> bool:
        return self.contains_pii and self.passed

    @property
    def false_alarm(self) -> bool:
        return not self.contains_pii and not self.passed


class Rate(BaseModel):
    model_config = ConfigDict(frozen=True)

    hits: int
    total: int
    rate: float


class Latency(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_p50_ms: float
    case_p95_ms: float
    dlp_calls: int
    dlp_p50_ms: float
    dlp_p95_ms: float
    dlp_retries: int


class PiiSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    cases: int
    score: Rate
    detection: Rate
    false_alarms: Rate
    negatives_with_findings: Rate
    negatives_with_masked_terms: Rate
    positives_with_masked_terms: Rate
    brand_preservation: Rate
    by_category: dict[str, Rate]
    by_type: dict[InfoType, Rate]
    preservation: dict[str, Rate]
    latency: Latency


def _present(fragment: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(fragment)}(?!\w)", text, re.IGNORECASE) is not None


def score(case: PiiCase, redaction: Redaction, seconds: float) -> CaseOutcome:
    leaked = tuple(
        Leak(value=item.value, info_type=item.info_type, fragments=fragments)
        for item in case.sensitive
        if (fragments := tuple(f for f in item.fragments if _present(f, redaction.text)))
    )
    leaked_values = {leak.value for leak in leaked}
    masked = tuple(item for item in case.preserve if item.value not in redaction.text)
    return CaseOutcome(
        id=case.id,
        category=case.category,
        contains_pii=case.contains_pii,
        text=case.text,
        redacted=redaction.text,
        findings=redaction.findings,
        leaked=leaked,
        masked=masked,
        removed_types=tuple(i.info_type for i in case.sensitive if i.value not in leaked_values),
        passed=not leaked if case.contains_pii else not (masked or redaction.findings),
        seconds=seconds,
    )


def _rate(flags: Iterable[bool]) -> Rate:
    values = list(flags)
    hits, total = sum(values), len(values)
    return Rate(hits=hits, total=total, rate=round(hits / total, 4) if total else 0.0)


def _by_type(outcomes: Sequence[CaseOutcome]) -> dict[InfoType, Rate]:
    flags: dict[InfoType, list[bool]] = defaultdict(list)
    for outcome in outcomes:
        for info_type in outcome.removed_types:
            flags[info_type].append(True)
        for leak in outcome.leaked:
            flags[leak.info_type].append(False)
    return {info_type: _rate(flags[info_type]) for info_type in InfoType if info_type in flags}


def _preservation(outcomes: Sequence[CaseOutcome], cases: Sequence[PiiCase]) -> dict[str, Rate]:
    masked = {outcome.id: {item.value for item in outcome.masked} for outcome in outcomes}
    flags: dict[str, list[bool]] = defaultdict(list)
    for case in cases:
        for item in case.preserve:
            flags[item.kind.value].append(item.value not in masked[case.id])
    return {kind: _rate(values) for kind, values in sorted(flags.items())}


def _brand_rate(outcomes: Sequence[CaseOutcome], cases: Sequence[PiiCase]) -> Rate:
    masked = {outcome.id: {item.value for item in outcome.masked} for outcome in outcomes}
    return _rate(
        item.value not in masked[case.id]
        for case in cases
        for item in case.preserve
        if item.kind in BRAND_KINDS
    )


def latency(outcomes: Sequence[CaseOutcome], dlp_seconds: Sequence[float], retries: int) -> Latency:
    cases = [outcome.seconds for outcome in outcomes]
    return Latency(
        case_p50_ms=round(middle(cases) * 1000, 2),
        case_p95_ms=round(percentile(cases, 0.95) * 1000, 2),
        dlp_calls=len(dlp_seconds),
        dlp_p50_ms=round(middle(dlp_seconds) * 1000, 1),
        dlp_p95_ms=round(percentile(dlp_seconds, 0.95) * 1000, 1),
        dlp_retries=retries,
    )


def summarize(
    cases: Sequence[PiiCase], outcomes: Sequence[CaseOutcome], timing: Latency
) -> PiiSummary:
    positives = [outcome for outcome in outcomes if outcome.contains_pii]
    negatives = [outcome for outcome in outcomes if not outcome.contains_pii]
    categories: dict[str, list[bool]] = defaultdict(list)
    for outcome in outcomes:
        categories[outcome.category].append(outcome.passed)
    return PiiSummary(
        cases=len(outcomes),
        score=_rate(outcome.passed for outcome in outcomes),
        detection=_rate(outcome.detected for outcome in positives),
        false_alarms=_rate(outcome.false_alarm for outcome in negatives),
        negatives_with_findings=_rate(bool(outcome.findings) for outcome in negatives),
        negatives_with_masked_terms=_rate(bool(outcome.masked) for outcome in negatives),
        positives_with_masked_terms=_rate(bool(outcome.masked) for outcome in positives),
        brand_preservation=_brand_rate(outcomes, cases),
        by_category={name: _rate(flags) for name, flags in categories.items()},
        by_type=_by_type(positives),
        preservation=_preservation(outcomes, cases),
        latency=timing,
    )
