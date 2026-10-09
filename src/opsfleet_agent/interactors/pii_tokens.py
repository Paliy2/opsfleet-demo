import hashlib
import hmac
import re
from collections.abc import Sequence
from uuid import UUID

from pydantic import SecretStr

from opsfleet_agent.core.errors import PiiServiceUnavailableError
from opsfleet_agent.entities.pii_tokens import TokenBook, TokenEntry, TokenizedText, TurnValue
from opsfleet_agent.entities.privacy import RedactedSpan, Redaction, substitute
from opsfleet_agent.ports.pii_token_vault import PiiTokenVault

_NON_DIGITS = re.compile(r"\D")
_DIGITS_ONLY_TYPES = frozenset({"PHONE_NUMBER", "CREDIT_CARD_NUMBER", "IBAN_CODE"})
_NUMBERING_ATTEMPTS = 3


def normalized_value(info_type: str, value: str) -> str:
    if info_type in _DIGITS_ONLY_TYPES:
        return _NON_DIGITS.sub("", value)
    return " ".join(value.casefold().split())


def fingerprint(key: bytes, info_type: str, value: str) -> str:
    message = f"{info_type}\x00{normalized_value(info_type, value)}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def numbering_key(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"opsfleet:pii-token-numbering", hashlib.sha256).digest()


def session_key(key: bytes, session_id: UUID) -> bytes:
    return hmac.new(key, session_id.bytes, hashlib.sha256).digest()


def tokenize(
    text: str, spans: Sequence[RedactedSpan], book: TokenBook, key: bytes
) -> TokenizedText:
    known = book.by_fingerprint()
    new_entries: list[TokenEntry] = []
    placeholders: dict[int, str] = {}
    values: list[TurnValue] = []
    for span in sorted(spans, key=lambda item: item.start):
        raw = span.value.get_secret_value()
        digest = fingerprint(key, span.info_type, raw)
        entry = known.get(digest)
        if entry is None:
            entry = TokenEntry(
                info_type=span.info_type,
                ordinal=book.next_ordinal(span.info_type),
                fingerprint=digest,
            )
            book = book.with_entry(entry)
            known[digest] = entry
            new_entries.append(entry)
        placeholders[span.start] = entry.placeholder
        values.append(
            TurnValue(
                label=entry.label,
                info_type=span.info_type,
                value=SecretStr(normalized_value(span.info_type, raw)),
            )
        )
    return TokenizedText(
        text=substitute(text, spans, lambda span: placeholders[span.start]),
        new_entries=tuple(new_entries),
        values=tuple(values),
    )


class PrivacyTokens:
    def __init__(self, vault: PiiTokenVault, key: bytes) -> None:
        self._vault = vault
        self._key = key

    async def number(self, session_id: UUID, text: str, redaction: Redaction) -> TokenizedText:
        if substitute(text, redaction.spans) != redaction.text:
            raise ValueError("redaction text does not match its spans")
        key = session_key(self._key, session_id)
        for _ in range(_NUMBERING_ATTEMPTS):
            book = await self._vault.load(session_id)
            tokenized = tokenize(text, redaction.spans, book, key)
            if not tokenized.new_entries:
                return tokenized
            if await self._vault.add(session_id, tokenized.new_entries):
                return tokenized
        raise PiiServiceUnavailableError("privacy token numbering kept conflicting")
