import re

import pytest
from google.api_core.exceptions import PermissionDenied, ServiceUnavailable
from google.cloud import dlp_v2

from evals.pii.brands import read_snapshot
from evals.pii.cases import InfoType, Keep, PiiCase, Preserved, Sensitive
from evals.pii.dlp import MeteredInspectClient
from evals.pii.generate import PII_DATASET, SEED, build_cases
from evals.pii.run import read_cases
from evals.pii.scoring import latency, score, summarize
from opsfleet_agent.entities.privacy import Redaction

NAME = Sensitive(
    value="Marla Thistlewood",
    info_type=InfoType.PERSON_NAME,
    fragments=("Marla", "Thistlewood"),
)
POSITIVE = PiiCase(
    id="pos-1",
    category="person_name",
    text="Send the Calvin Klein report to Marla Thistlewood.",
    contains_pii=True,
    expected_info_types=(InfoType.PERSON_NAME,),
    sensitive=(NAME,),
    preserve=(Preserved(value="Calvin Klein", kind=Keep.BRAND),),
)
NEGATIVE = PiiCase(
    id="neg-1",
    category="brand_person_like",
    text="Compare Ralph Lauren vs Tommy Hilfiger.",
    contains_pii=False,
    expected_info_types=(),
    sensitive=(),
    preserve=(
        Preserved(value="Ralph Lauren", kind=Keep.BRAND),
        Preserved(value="Tommy Hilfiger", kind=Keep.BRAND),
    ),
)


def redacted(text: str, *findings: str) -> Redaction:
    return Redaction(text=text, findings=findings)


def test_fully_masked_positive_is_detected() -> None:
    outcome = score(
        POSITIVE, redacted("Send the Calvin Klein report to [PERSON_NAME].", "PERSON_NAME"), 0.1
    )

    assert outcome.detected
    assert outcome.removed_types == (InfoType.PERSON_NAME,)
    assert not outcome.masked


def test_half_masked_name_still_leaks() -> None:
    outcome = score(POSITIVE, redacted("Send it to [PERSON_NAME] Thistlewood.", "PERSON_NAME"), 0)

    assert not outcome.passed
    assert outcome.leaked[0].fragments == ("Thistlewood",)
    assert [item.value for item in outcome.masked] == ["Calvin Klein"]


def test_fragment_inside_a_longer_word_is_not_a_leak() -> None:
    outcome = score(POSITIVE, redacted("Marlaxx report to [PERSON_NAME]. Calvin Klein"), 0)

    assert outcome.detected


def test_negative_with_any_finding_is_a_false_alarm() -> None:
    untouched = score(NEGATIVE, redacted(NEGATIVE.text), 0)
    flagged = score(NEGATIVE, redacted(NEGATIVE.text, "PHONE_NUMBER"), 0)
    masked = score(NEGATIVE, redacted("Compare [PERSON_NAME] vs Tommy Hilfiger."), 0)

    assert untouched.passed
    assert flagged.false_alarm
    assert masked.false_alarm


def test_summary_rates() -> None:
    outcomes = [
        score(POSITIVE, redacted("Send the Calvin Klein report to [PERSON_NAME].", "X"), 0.2),
        score(NEGATIVE, redacted("Compare [PERSON_NAME] vs Tommy Hilfiger.", "X"), 0.4),
    ]

    summary = summarize([POSITIVE, NEGATIVE], outcomes, latency(outcomes, [0.3, 0.5], 1))

    assert summary.score.rate == 0.5
    assert summary.detection.rate == 1.0
    assert summary.false_alarms.hits == 1
    assert summary.brand_preservation.hits == 2
    assert summary.brand_preservation.total == 3
    assert summary.by_type[InfoType.PERSON_NAME].rate == 1.0
    assert summary.latency.dlp_p95_ms == 500.0
    assert summary.latency.dlp_retries == 1


def test_case_rejects_values_missing_from_text() -> None:
    with pytest.raises(ValueError, match="not in text"):
        PiiCase(
            id="bad",
            category="person_name",
            text="nobody here",
            contains_pii=True,
            expected_info_types=(InfoType.PERSON_NAME,),
            sensitive=(NAME,),
        )


def test_dataset_is_reproducible_from_the_generator() -> None:
    cases = build_cases(SEED, read_snapshot())

    assert cases == read_cases(PII_DATASET, set(), set())
    assert len(cases) == 500
    assert sum(case.contains_pii for case in cases) == 300


def luhn_ok(number: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(number)):
        total += sum(divmod(int(char) * (2 if index % 2 else 1), 10))
    return total % 10 == 0


def test_dataset_cards_respect_luhn() -> None:
    cases = read_cases(PII_DATASET, {"credit_card", "card_lookalike"}, set())
    valid = [i.value for c in cases for i in c.sensitive]
    lookalikes = [i.value for c in cases for i in c.preserve if i.kind is Keep.NUMBER]

    assert len(valid) == 25
    assert len(lookalikes) == 20
    assert all(luhn_ok(re.sub(r"\D", "", value)) for value in valid)
    assert not any(luhn_ok(re.sub(r"\D", "", value)) for value in lookalikes)


class FlakyDlp:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        self.calls = 0

    async def inspect_content(
        self, request: dlp_v2.InspectContentRequest
    ) -> dlp_v2.InspectContentResponse:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return dlp_v2.InspectContentResponse()


async def test_meter_retries_transient_errors_and_times_successes() -> None:
    client = FlakyDlp([ServiceUnavailable("busy")])
    meter = MeteredInspectClient(client, backoff_seconds=(0, 0))

    await meter.inspect_content(dlp_v2.InspectContentRequest())

    assert client.calls == 2
    assert meter.retries == 1
    assert len(meter.seconds) == 1


async def test_meter_does_not_retry_permanent_errors() -> None:
    meter = MeteredInspectClient(FlakyDlp([PermissionDenied("no")]), (0, 0))

    with pytest.raises(PermissionDenied):
        await meter.inspect_content(dlp_v2.InspectContentRequest())

    assert meter.retries == 0
