import re

import pytest
from pydantic import SecretStr

from opsfleet_agent.entities.privacy import (
    PLACEHOLDER_INFO_TYPES,
    RedactedSpan,
    Redaction,
    is_masked,
    placeholders_in,
    substitute,
)
from opsfleet_agent.interactors.pii_redaction import LayeredRedactor, RegexRedactor


@pytest.mark.parametrize(
    ("text", "expected", "findings"),
    [
        ("my email is eugene dot domer @ gmail dot com", "my email is [EMAIL_ADDRESS]", 1),
        ("reach me at eugene.domer(at)gmail(dot)com ok", "reach me at [EMAIL_ADDRESS] ok", 1),
        ("eugene [at] gmail [dot] com", "[EMAIL_ADDRESS]", 1),
        ("email me at john@x.com at noon", "email me at [EMAIL_ADDRESS] at noon", 1),
        ("card 4111 1111 1111 1111 exp", "card [CREDIT_CARD_NUMBER] exp", 1),
        ("call +1 415 555 0134", "call [PHONE_NUMBER]", 1),
        (
            "my phone is four one five, five five five, zero one three four",
            "my phone is [PHONE_NUMBER]",
            1,
        ),
        ("pin 37.7749, -122.4194", "pin [LOCATION_COORDINATES]", 1),
    ],
)
async def test_regex_layer_catches_plain_and_obfuscated_pii(
    text: str, expected: str, findings: int
) -> None:
    redaction = await RegexRedactor().redact(text)

    assert redaction.text == expected
    assert len(redaction.findings) == findings
    assert set(redaction.findings) <= PLACEHOLDER_INFO_TYPES
    assert is_masked(redaction.text)


@pytest.mark.parametrize(
    "text",
    [
        "Compare revenue at Diesel. In Q3 it grew",
        "look at sales for 2019 2020 2021 2022",
        "top one two three brands in 2024",
        "customer user_1a2b3c4d@gmail.com spent most",
        "what is the growth at Calvin Klein vs Diesel.com",
    ],
)
async def test_regex_layer_leaves_business_text_and_masked_ids(text: str) -> None:
    assert await RegexRedactor().redact(text) == Redaction(text=text)


class NameLayer:
    def __init__(self, name: str = "Ann", info_type: str = "PERSON_NAME") -> None:
        self.name = name
        self.info_type = info_type
        self.seen: list[str] = []

    async def redact(self, text: str) -> Redaction:
        self.seen.append(text)
        spans = tuple(
            RedactedSpan(
                start=match.start(),
                end=match.end(),
                info_type=self.info_type,
                value=SecretStr(match.group(0)),
            )
            for match in re.finditer(re.escape(self.name), text)
        )
        return Redaction(text=substitute(text, spans), findings=(self.info_type,), spans=spans)


class SpanlessLayer:
    async def redact(self, text: str) -> Redaction:
        return Redaction(text=text.replace("Ann", "[PERSON_NAME]"), findings=("PERSON_NAME",))


def values(redaction: Redaction) -> list[tuple[str, str]]:
    return [(span.info_type, span.value.get_secret_value()) for span in redaction.spans]


async def test_layers_run_in_order_and_merge_findings() -> None:
    redactor = LayeredRedactor([RegexRedactor(), NameLayer()])

    redaction = await redactor.redact("Ann, a@b.co")

    assert redaction.text == "[PERSON_NAME], [EMAIL_ADDRESS]"
    assert redaction.findings == ("EMAIL_ADDRESS", "PERSON_NAME")


async def test_later_layers_see_earlier_redactions_and_spans_map_to_the_original() -> None:
    names = NameLayer("Jane Roe")
    text = "mail jane.roe@example.org and ask Jane Roe, then call +1 415 555 0134"

    redaction = await LayeredRedactor([RegexRedactor(), names]).redact(text)

    assert names.seen == ["mail [EMAIL_ADDRESS] and ask Jane Roe, then call [PHONE_NUMBER]"]
    assert redaction.text == "mail [EMAIL_ADDRESS] and ask [PERSON_NAME], then call [PHONE_NUMBER]"
    assert values(redaction) == [
        ("EMAIL_ADDRESS", "jane.roe@example.org"),
        ("PERSON_NAME", "Jane Roe"),
        ("PHONE_NUMBER", "+1 415 555 0134"),
    ]
    assert substitute(text, redaction.spans) == redaction.text


async def test_a_later_finding_that_covers_a_placeholder_absorbs_it() -> None:
    covering = NameLayer("Ann [EMAIL_ADDRESS]")

    redaction = await LayeredRedactor([RegexRedactor(), covering]).redact("hi Ann a@b.co ok")

    assert redaction.text == "hi [PERSON_NAME] ok"
    assert values(redaction) == [("PERSON_NAME", "Ann a@b.co")]


async def test_a_layer_whose_text_does_not_match_its_spans_fails_fast() -> None:
    with pytest.raises(ValueError, match="does not match"):
        await LayeredRedactor([RegexRedactor(), SpanlessLayer()]).redact("Ann, a@b.co")


async def test_regex_spans_carry_the_original_values() -> None:
    text = "mail john@x.com or call +1 415 555 0134"

    redaction = await RegexRedactor().redact(text)

    assert values(redaction) == [
        ("EMAIL_ADDRESS", "john@x.com"),
        ("PHONE_NUMBER", "+1 415 555 0134"),
    ]
    assert "john@x.com" not in repr(redaction)


def test_numbered_placeholders_are_recognised() -> None:
    assert is_masked("orders of [PERSON_NAME_12]")
    assert placeholders_in("[PERSON_NAME_1] and [email_address_2]") == (
        "[EMAIL_ADDRESS_2]",
        "[PERSON_NAME_1]",
    )
    assert not is_masked("orders of [PERSON_NAME_0]")
    assert not is_masked("orders of [CUSTOMER_1]")
