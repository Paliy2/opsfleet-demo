import re
from collections.abc import Awaitable, Callable
from typing import Protocol

import anyio
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import dlp_v2

from opsfleet_agent.core.errors import PiiServiceUnavailableError
from opsfleet_agent.entities.privacy import (
    PSEUDONYM_FIRST_NAMES,
    PSEUDONYM_LAST_NAMES,
    Redaction,
)
from opsfleet_agent.ports.privacy import PiiRedactor

_INFO_TYPES = (
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
_STRICT_INFO_TYPES = ("PERSON_NAME", "STREET_ADDRESS", "DATE_OF_BIRTH")
_NAME_INFO_TYPES = frozenset({"PERSON_NAME"})
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
        info_types=[dlp_v2.InfoType(name=name) for name in _INFO_TYPES],
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


def _term_pattern(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    phrases = sorted({term.strip() for term in terms if term.strip()}, key=len, reverse=True)
    if not phrases:
        return None
    return re.compile(rf"(?<!\w)(?:{'|'.join(map(re.escape, phrases))})(?!\w)", re.IGNORECASE)


def _protected(text: str, terms: re.Pattern[str] | None) -> tuple[Span, ...]:
    patterns = (_PSEUDONYM, terms) if terms is not None else (_PSEUDONYM,)
    return tuple(match.span() for pattern in patterns for match in pattern.finditer(text))


def _inside(span: Span, protected: tuple[Span, ...]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in protected)


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
        self._terms: re.Pattern[str] | None = None
        self._loaded = False
        self._lock = anyio.Lock()
        self._timeout = timeout_seconds

    async def redact(self, text: str) -> Redaction:
        if not text.strip():
            return Redaction(text=text)
        response = await self._inspect(text)
        protected = _protected(text, await self._term_matcher())
        findings = [
            ((finding.location.codepoint_range.start, finding.location.codepoint_range.end), name)
            for finding in response.result.findings
            if (name := finding.info_type.name)
        ]
        kept = _without_overlaps(
            [
                (span, name)
                for span, name in findings
                if name not in _NAME_INFO_TYPES or not _inside(span, protected)
            ]
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

    async def _term_matcher(self) -> re.Pattern[str] | None:
        async with self._lock:
            if not self._loaded:
                self._terms = _term_pattern(await self._allowed_terms())
                self._loaded = True
            return self._terms
