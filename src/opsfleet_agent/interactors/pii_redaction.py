import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from opsfleet_agent.entities.privacy import Redaction, is_masked_email
from opsfleet_agent.ports.privacy import PiiRedactor

_DOT = r"(?:\.|\s*\bdot\b\s*|\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*)"
_AT = r"(?:\s*@\s*|\s+at\s+|\s*[\(\[\{]\s*at\s*[\)\]\}]\s*)"
_DIGIT_WORD = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_DIGIT_WORD_PATTERN = re.compile(_DIGIT_WORD, re.IGNORECASE)
_MIN_DIGIT_WORDS = 4
_CARD_LENGTHS = range(13, 20)


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        total += sum(divmod(int(char) * (2 if index % 2 else 1), 10))
    return total % 10 == 0


def _is_card(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    return len(digits) in _CARD_LENGTHS and _luhn_valid(digits)


def _not_masked_email(candidate: str) -> bool:
    return not is_masked_email(candidate)


def _has_digit_words(candidate: str) -> bool:
    return len(_DIGIT_WORD_PATTERN.findall(candidate)) >= _MIN_DIGIT_WORDS


def _always(_candidate: str) -> bool:
    return True


@dataclass(frozen=True)
class _Rule:
    info_type: str
    pattern: re.Pattern[str]
    applies: Callable[[str], bool] = _always


_RULES = (
    _Rule(
        "EMAIL_ADDRESS",
        re.compile(
            rf"\b[\w+-]+(?:{_DOT}[\w+-]+)*{_AT}[\w-]+(?:{_DOT}[\w-]+)*{_DOT}[a-z]{{2,6}}\b"
            r"(?!\s*[@\(\[\{]|\.\w|\s+dot\b)",
            re.IGNORECASE,
        ),
        _not_masked_email,
    ),
    _Rule("LOCATION_COORDINATES", re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")),
    _Rule(
        "PHONE_NUMBER",
        re.compile(
            r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])"
            r"|(?<![\w-])\+\d{6,14}(?![\w-])"
        ),
    ),
    _Rule(
        "PHONE_NUMBER",
        re.compile(
            rf"\b(?:{_DIGIT_WORD}|\d)(?:[\s,.-]+(?:{_DIGIT_WORD}|\d)){{6,}}\b", re.IGNORECASE
        ),
        _has_digit_words,
    ),
    _Rule("CREDIT_CARD_NUMBER", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), _is_card),
)


class RegexRedactor(PiiRedactor):
    async def redact(self, text: str) -> Redaction:
        findings: set[str] = set()
        for rule in _RULES:
            text = rule.pattern.sub(partial(_apply, rule, findings), text)
        return Redaction(text=text, findings=tuple(sorted(findings)))


def _apply(rule: _Rule, findings: set[str], match: re.Match[str]) -> str:
    if not rule.applies(match.group(0)):
        return match.group(0)
    findings.add(rule.info_type)
    return f"[{rule.info_type}]"


class LayeredRedactor(PiiRedactor):
    def __init__(self, layers: Sequence[PiiRedactor]) -> None:
        self._layers = tuple(layers)

    async def redact(self, text: str) -> Redaction:
        findings: set[str] = set()
        for layer in self._layers:
            redaction = await layer.redact(text)
            text = redaction.text
            findings.update(redaction.findings)
        return Redaction(text=text, findings=tuple(sorted(findings)))
