import re
from collections.abc import Callable, Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

PSEUDONYM_FIRST_NAMES = (
    "Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn",
    "Drew", "Robin", "Skyler", "Reese", "Rowan", "Emery", "Finley", "Harper", "Kai", "Parker",
)  # fmt: skip
PSEUDONYM_LAST_NAMES = (
    "Rivera", "Chen", "Novak", "Okafor", "Silva", "Kowalski", "Haddad", "Larsen", "Moreau", "Sato",
    "Petrov", "Mensah", "Costa", "Fischer", "Nguyen", "Duarte", "Kaur", "Lindqvist", "Abe", "Ruiz",
)  # fmt: skip


class RedactedSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int
    info_type: str = Field(pattern=r"^[A-Z][A-Z_]*[A-Z]$")
    value: SecretStr

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if self.end <= self.start:
            raise ValueError("a redacted span covers at least one character")
        return self


class Redaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    findings: tuple[str, ...] = ()
    spans: tuple[RedactedSpan, ...] = ()


def bare_placeholder(span: RedactedSpan) -> str:
    return f"[{span.info_type}]"


def substitute(
    text: str,
    spans: Sequence[RedactedSpan],
    render: Callable[[RedactedSpan], str] = bare_placeholder,
) -> str:
    pieces: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda item: item.start):
        if span.start < cursor or span.end > len(text):
            raise ValueError("redacted spans overlap or fall outside the text")
        pieces.extend((text[cursor : span.start], render(span)))
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces)


DLP_INFO_TYPES = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "PERSON_NAME",
    "STREET_ADDRESS",
    "CREDIT_CARD_NUMBER",
    "IBAN_CODE",
    "US_SOCIAL_SECURITY_NUMBER",
    "PASSPORT",
    "IP_ADDRESS",
    "DATE_OF_BIRTH",
)
PLACEHOLDER_INFO_TYPES = frozenset({*DLP_INFO_TYPES, "LOCATION_COORDINATES"})
_PLACEHOLDER = re.compile(
    rf"\[(?:{'|'.join(sorted(PLACEHOLDER_INFO_TYPES))})(?:_[1-9]\d*)?\]", re.IGNORECASE
)


def placeholders_in(text: str) -> tuple[str, ...]:
    return tuple(sorted({match.upper() for match in _PLACEHOLDER.findall(text)}))


def is_masked(text: str | None) -> bool:
    return bool(text and _PLACEHOLDER.search(text))


_MASKED_EMAIL = re.compile(r"^user_[0-9a-f]{8}@", re.IGNORECASE)


def is_masked_email(text: str) -> bool:
    return _MASKED_EMAIL.match(text) is not None
