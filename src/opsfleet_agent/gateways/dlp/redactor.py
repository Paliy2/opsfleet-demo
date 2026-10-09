import re
from collections.abc import Awaitable, Callable
from typing import Protocol

import anyio
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import dlp_v2

from opsfleet_agent.core.errors import PiiServiceUnavailableError
from opsfleet_agent.entities.privacy import (
    DLP_INFO_TYPES,
    PSEUDONYM_FIRST_NAMES,
    PSEUDONYM_LAST_NAMES,
    Redaction,
    is_masked_email,
)
from opsfleet_agent.ports.privacy import PiiRedactor

_STRICT_INFO_TYPES = ("PERSON_NAME", "STREET_ADDRESS", "DATE_OF_BIRTH")
_NAME_INFO_TYPES = frozenset({"PERSON_NAME"})
_EMAIL_INFO_TYPE = "EMAIL_ADDRESS"
_WORD = re.compile(r"\w(?:[\w&'.-]*\w)?")
_EXACT_ONLY_CHARS = 5
_ONE_TYPO_CHARS = 10
_PSEUDONYM = re.compile(
    rf"\b(?:{'|'.join(PSEUDONYM_FIRST_NAMES)})\s+(?:{'|'.join(PSEUDONYM_LAST_NAMES)})\b",
    re.IGNORECASE,
)

AllowedTerms = Callable[[], Awaitable[tuple[str, ...]]]
Span = tuple[int, int]


class InspectClient(Protocol):
    async def inspect_content(
        self, request: dlp_v2.InspectContentRequest
    ) -> dlp_v2.InspectContentResponse: ...


def build_request(parent: str, text: str) -> dlp_v2.InspectContentRequest:
    inspect = dlp_v2.InspectConfig(
        info_types=[dlp_v2.InfoType(name=name) for name in DLP_INFO_TYPES],
        min_likelihood=dlp_v2.Likelihood.POSSIBLE,
        min_likelihood_per_info_type=[
            dlp_v2.InspectConfig.InfoTypeLikelihood(
                info_type=dlp_v2.InfoType(name=name),
                min_likelihood=dlp_v2.Likelihood.LIKELY,
            )
            for name in _STRICT_INFO_TYPES
        ],
        include_quote=False,
    )
    return dlp_v2.InspectContentRequest(
        parent=parent, inspect_config=inspect, item=dlp_v2.ContentItem(value=text)
    )


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _typos_allowed(term: str) -> int:
    if len(term) < _EXACT_ONLY_CHARS:
        return 0
    return 1 if len(term) <= _ONE_TYPO_CHARS else 2


def _within_edits(left: str, right: str, limit: int) -> bool:
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            substitution = previous[column - 1] + (left_char != right_char)
            current.append(min(previous[column] + 1, current[column - 1] + 1, substitution))
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _overlaps(left: Span, right: Span) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _window_starts(touched: list[int], count: int, total: int) -> range:
    return range(max(0, touched[-1] - count + 1), min(touched[0], total - count) + 1)


class _TermMatcher:
    def __init__(self, terms: tuple[str, ...]) -> None:
        phrases = sorted({term.strip() for term in terms if term.strip()}, key=len, reverse=True)
        self._exact = (
            re.compile(rf"(?<!\w)(?:{'|'.join(map(re.escape, phrases))})(?!\w)", re.IGNORECASE)
            if phrases
            else None
        )
        by_words: dict[int, list[str]] = {}
        for phrase in map(_normalized, phrases):
            if _typos_allowed(phrase):
                by_words.setdefault(len(phrase.split()), []).append(phrase)
        self._fuzzy = {words: tuple(group) for words, group in by_words.items()}

    def exact_spans(self, text: str) -> tuple[Span, ...]:
        patterns = (_PSEUDONYM, self._exact) if self._exact is not None else (_PSEUDONYM,)
        return tuple(match.span() for pattern in patterns for match in pattern.finditer(text))

    def misspelled_spans(self, text: str, span: Span) -> tuple[Span, ...]:
        words = [match.span() for match in _WORD.finditer(text)]
        touched = [index for index, word in enumerate(words) if _overlaps(word, span)]
        if not touched:
            return ()
        windows: list[Span] = []
        for count, group in self._fuzzy.items():
            for first in _window_starts(touched, count, len(words)):
                window = (words[first][0], words[first + count - 1][1])
                if self._close(text[window[0] : window[1]], group):
                    windows.append(window)
        return tuple(windows)

    @staticmethod
    def _close(candidate: str, group: tuple[str, ...]) -> bool:
        window = _normalized(candidate)
        return any(_within_edits(window, term, _typos_allowed(term)) for term in group)


def _inside(span: Span, protected: tuple[Span, ...]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in protected)


def _is_brand_or_pseudonym(text: str, span: Span, terms: _TermMatcher) -> bool:
    return _inside(span, terms.exact_spans(text)) or _inside(
        span, terms.misspelled_spans(text, span)
    )


def _is_allowed(text: str, span: Span, info_type: str, terms: _TermMatcher) -> bool:
    if info_type == _EMAIL_INFO_TYPE:
        return is_masked_email(text[span[0] : span[1]])
    return info_type in _NAME_INFO_TYPES and _is_brand_or_pseudonym(text, span, terms)


def _redact(text: str, findings: list[tuple[Span, str]]) -> str:
    for (start, end), info_type in sorted(findings, reverse=True):
        text = f"{text[:start]}[{info_type}]{text[end:]}"
    return text


def _without_overlaps(findings: list[tuple[Span, str]]) -> list[tuple[Span, str]]:
    kept: list[tuple[Span, str]] = []
    for span, info_type in sorted(findings, key=lambda item: (item[0][0], -item[0][1])):
        if kept and span[0] < kept[-1][0][1]:
            continue
        kept.append((span, info_type))
    return kept


class DlpRedactor(PiiRedactor):
    def __init__(
        self,
        client: InspectClient,
        *,
        project: str,
        location: str,
        allowed_terms: AllowedTerms,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._parent = f"projects/{project}/locations/{location}"
        self._allowed_terms = allowed_terms
        self._terms: _TermMatcher | None = None
        self._lock = anyio.Lock()
        self._timeout = timeout_seconds

    async def redact(self, text: str) -> Redaction:
        if not text.strip():
            return Redaction(text=text)
        response = await self._inspect(text)
        terms = await self._term_matcher()
        findings = [
            ((finding.location.codepoint_range.start, finding.location.codepoint_range.end), name)
            for finding in response.result.findings
            if (name := finding.info_type.name)
        ]
        kept = _without_overlaps(
            [(span, name) for span, name in findings if not _is_allowed(text, span, name, terms)]
        )
        return Redaction(
            text=_redact(text, kept), findings=tuple(sorted({name for _, name in kept}))
        )

    async def _inspect(self, text: str) -> dlp_v2.InspectContentResponse:
        try:
            with anyio.fail_after(self._timeout):
                return await self._client.inspect_content(build_request(self._parent, text))
        except GoogleAPICallError as error:
            raise PiiServiceUnavailableError(f"DLP inspect failed: {error.message}") from error
        except TimeoutError as error:
            raise PiiServiceUnavailableError("DLP inspect timed out") from error

    async def _term_matcher(self) -> _TermMatcher:
        async with self._lock:
            if self._terms is None:
                self._terms = _TermMatcher(await self._allowed_terms())
            return self._terms
