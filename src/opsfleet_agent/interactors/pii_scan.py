import re

from opsfleet_agent.core.errors import PiiDetectedError

_MASKED_EMAIL = re.compile(r"^user_[0-9a-f]{8}@")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w.])")
_MIN_PHONE_DIGITS = 10
_PRECISE_COORDINATES = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")


def _looks_like_phone(candidate: str) -> bool:
    return sum(char.isdigit() for char in candidate) >= _MIN_PHONE_DIGITS


def ensure_no_pii(text: str) -> str:
    for email in _EMAIL.findall(text):
        if not _MASKED_EMAIL.match(email):
            raise PiiDetectedError("unmasked email address in output")
    if _PRECISE_COORDINATES.search(text):
        raise PiiDetectedError("precise coordinates in output")
    if any(_looks_like_phone(candidate) for candidate in _PHONE.findall(text)):
        raise PiiDetectedError("phone-like number in output")
    return text
