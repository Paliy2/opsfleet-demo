import re

from opsfleet_agent.core.errors import PiiDetectedError

_MASKED_EMAIL = re.compile(r"^user_[0-9a-f]{8}@")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(
    r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])"
    r"|(?<![\w-])\+\d{6,14}(?![\w-])"
)
_PRECISE_COORDINATES = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")


def ensure_no_pii(text: str) -> str:
    for email in _EMAIL.findall(text):
        if not _MASKED_EMAIL.match(email):
            raise PiiDetectedError("unmasked email address in output")
    if _PRECISE_COORDINATES.search(text):
        raise PiiDetectedError("precise coordinates in output")
    if _PHONE.search(text):
        raise PiiDetectedError("phone number in output")
    return text
