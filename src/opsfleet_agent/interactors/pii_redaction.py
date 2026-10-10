import ipaddress
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import SecretStr

from opsfleet_agent.entities.privacy import (
    RedactedSpan,
    Redaction,
    bare_placeholder,
    is_masked_email,
    substitute,
)
from opsfleet_agent.ports.privacy import PiiRedactor

_DOT = r"(?:\.|\s+\.\s+|\s*\bdot\b\s*|\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*)"
_AT = r"(?:\s*@\s*|\s+at\s+|\s*[\(\[\{]\s*at\s*[\)\]\}]\s*)"
_DIGIT_WORD = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_DIGIT_WORD_PATTERN = re.compile(_DIGIT_WORD, re.IGNORECASE)
_MIN_DIGIT_WORDS = 4
_CARD_LENGTHS = range(13, 20)
_INVALID_SSN_AREAS = frozenset({"000", "666"})


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        total += sum(divmod(int(char) * (2 if index % 2 else 1), 10))
    return total % 10 == 0


def _is_card(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    return len(digits) in _CARD_LENGTHS and _luhn_valid(digits)


def _is_ipv6(candidate: str) -> bool:
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return False
    return True


def _is_ssn(candidate: str) -> bool:
    area, group, serial = re.split(r"[ -]", candidate)
    return area not in _INVALID_SSN_AREAS and area[0] != "9" and group != "00" and serial != "0000"


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
    _Rule(
        "IP_ADDRESS",
        re.compile(r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![\w:])", re.IGNORECASE),
        _is_ipv6,
    ),
    _Rule(
        "US_SOCIAL_SECURITY_NUMBER",
        re.compile(r"(?<![\w-])\d{3}([ -])\d{2}\1\d{4}(?![\w-])"),
        _is_ssn,
    ),
    _Rule("CREDIT_CARD_NUMBER", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), _is_card),
)


def _overlaps(span: RedactedSpan, kept: Sequence[RedactedSpan]) -> bool:
    return any(span.start < other.end and other.start < span.end for other in kept)


def _rule_spans(rule: _Rule, text: str) -> list[RedactedSpan]:
    return [
        RedactedSpan(
            start=match.start(),
            end=match.end(),
            info_type=rule.info_type,
            value=SecretStr(match.group(0)),
        )
        for match in rule.pattern.finditer(text)
        if rule.applies(match.group(0))
    ]


def _redaction(text: str, spans: Sequence[RedactedSpan]) -> Redaction:
    ordered = tuple(sorted(spans, key=lambda span: span.start))
    return Redaction(
        text=substitute(text, ordered),
        findings=tuple(sorted({span.info_type for span in ordered})),
        spans=ordered,
    )


class RegexRedactor(PiiRedactor):
    async def redact(self, text: str) -> Redaction:
        kept: list[RedactedSpan] = []
        for rule in _RULES:
            kept.extend(span for span in _rule_spans(rule, text) if not _overlaps(span, kept))
        return _redaction(text, kept)


def _to_original(position: int, spans: Sequence[RedactedSpan], *, end: bool) -> int:
    shift = 0
    for span in spans:
        placeholder_end = span.start + shift + len(bare_placeholder(span))
        if position < span.start + shift:
            break
        if position < placeholder_end or (end and position == placeholder_end):
            return span.end if end else span.start
        shift += len(bare_placeholder(span)) - (span.end - span.start)
    return position - shift


def _merge(original: str, earlier: list[RedactedSpan], later: Redaction) -> list[RedactedSpan]:
    merged = list(earlier)
    for span in later.spans:
        start = _to_original(span.start, earlier, end=False)
        end = _to_original(span.end, earlier, end=True)
        merged = [kept for kept in merged if kept.end <= start or end <= kept.start]
        merged.append(
            RedactedSpan(
                start=start, end=end, info_type=span.info_type, value=SecretStr(original[start:end])
            )
        )
    return sorted(merged, key=lambda span: span.start)


class LayeredRedactor(PiiRedactor):
    def __init__(self, layers: Sequence[PiiRedactor]) -> None:
        self._layers = tuple(layers)

    async def redact(self, text: str) -> Redaction:
        spans: list[RedactedSpan] = []
        current = text
        for layer in self._layers:
            redaction = await layer.redact(current)
            if substitute(current, redaction.spans) != redaction.text:
                raise ValueError("redaction layer returned text that does not match its spans")
            spans = _merge(text, spans, redaction)
            current = substitute(text, spans)
        return _redaction(text, spans)
