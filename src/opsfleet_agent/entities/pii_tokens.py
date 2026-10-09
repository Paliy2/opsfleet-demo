import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from opsfleet_agent.entities.privacy import PLACEHOLDER_INFO_TYPES

PLACEHOLDER_LABEL = rf"(?:{'|'.join(sorted(PLACEHOLDER_INFO_TYPES))})_[1-9]\d*"
NUMBERED_TOKEN = re.compile(rf"\[{PLACEHOLDER_LABEL}\]")
_TOKEN_TERM = re.compile(rf"^\s*\[?({PLACEHOLDER_LABEL})\]?\s*$", re.IGNORECASE)
_PSEUDONYMOUS_ID = r"^[0-9a-f]{16}$"
_MASKED_EMAIL = r"^user_[0-9a-f]{8}@[^@\s]+$"


def is_token_term(term: str) -> bool:
    return bool(_TOKEN_TERM.match(term))


def token_label(term: str) -> str:
    match = _TOKEN_TERM.match(term)
    if match is None:
        raise ValueError(f"not a privacy token: {term!r}")
    return match.group(1).upper()


class TokenEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    info_type: str
    ordinal: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def label(self) -> str:
        return f"{self.info_type}_{self.ordinal}"

    @property
    def placeholder(self) -> str:
        return f"[{self.label}]"


class TokenBook(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[TokenEntry, ...] = ()

    def by_fingerprint(self) -> dict[str, TokenEntry]:
        return {entry.fingerprint: entry for entry in self.entries}

    def next_ordinal(self, info_type: str) -> int:
        return 1 + max((e.ordinal for e in self.entries if e.info_type == info_type), default=0)

    def with_entry(self, entry: TokenEntry) -> Self:
        return self.model_copy(update={"entries": (*self.entries, entry)})


class TurnValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    info_type: str
    value: SecretStr


class TokenizedText(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    new_entries: tuple[TokenEntry, ...] = ()
    values: tuple[TurnValue, ...] = ()

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.label for value in self.values))


class LookupPurpose(StrEnum):
    SEARCH = "search"
    DELETE = "delete"


class BlindOperator(StrEnum):
    EXACT = "exact"
    PREFIX = "prefix"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    FUZZY = "fuzzy"
    SOUNDS_LIKE = "sounds_like"


class BlindTerm(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator: BlindOperator
    info_type: str
    label: str
    values: tuple[SecretStr, ...] = Field(min_length=1, max_length=2)
    max_distance: int = Field(default=0, ge=0, le=2)


class CustomerMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=_PSEUDONYMOUS_ID)
    first_name: str
    last_name: str
    email: str = Field(pattern=_MASKED_EMAIL)
    match_kind: BlindOperator

    @property
    def report_terms(self) -> tuple[str, str]:
        return f"user_{self.id[:8]}@", self.id

    @property
    def display(self) -> str:
        return f"{self.first_name} {self.last_name} ({self.email})"


class MatchedCustomer(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: str
    customer: CustomerMatch


class TokenResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    info_type: str
    customers: tuple[CustomerMatch, ...]
