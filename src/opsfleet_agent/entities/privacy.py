import re

from pydantic import BaseModel, ConfigDict

PSEUDONYM_FIRST_NAMES = (
    "Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn",
    "Drew", "Robin", "Skyler", "Reese", "Rowan", "Emery", "Finley", "Harper", "Kai", "Parker",
)  # fmt: skip
PSEUDONYM_LAST_NAMES = (
    "Rivera", "Chen", "Novak", "Okafor", "Silva", "Kowalski", "Haddad", "Larsen", "Moreau", "Sato",
    "Petrov", "Mensah", "Costa", "Fischer", "Nguyen", "Duarte", "Kaur", "Lindqvist", "Abe", "Ruiz",
)  # fmt: skip


class Redaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    findings: tuple[str, ...] = ()


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
_PLACEHOLDER = re.compile(rf"\[(?:{'|'.join(sorted(PLACEHOLDER_INFO_TYPES))})\]", re.IGNORECASE)


def placeholders_in(text: str) -> tuple[str, ...]:
    return tuple(sorted({match.upper() for match in _PLACEHOLDER.findall(text)}))


def is_masked(text: str | None) -> bool:
    return bool(text and _PLACEHOLDER.search(text))


_MASKED_EMAIL = re.compile(r"^user_[0-9a-f]{8}@", re.IGNORECASE)


def is_masked_email(text: str) -> bool:
    return _MASKED_EMAIL.match(text) is not None
