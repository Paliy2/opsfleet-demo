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


_MASK_TOKEN = re.compile(r"\[[A-Z_]+\]")


def is_masked(text: str | None) -> bool:
    return bool(text and _MASK_TOKEN.search(text))
