import pytest

from opsfleet_agent.entities.privacy import Redaction
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


class Upper:
    async def redact(self, text: str) -> Redaction:
        return Redaction(text=text.replace("Ann", "[PERSON_NAME]"), findings=("PERSON_NAME",))


async def test_layers_run_in_order_and_merge_findings() -> None:
    redactor = LayeredRedactor([RegexRedactor(), Upper()])

    redaction = await redactor.redact("Ann, a@b.co")

    assert redaction.text == "[PERSON_NAME], [EMAIL_ADDRESS]"
    assert redaction.findings == ("EMAIL_ADDRESS", "PERSON_NAME")
