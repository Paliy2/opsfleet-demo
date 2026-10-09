from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.pii_tokens import (
    BlindTerm,
    CustomerMatch,
    LookupPurpose,
    TokenBook,
    TokenEntry,
    TurnValue,
)


class PiiTokenVault(Protocol):
    async def load(self, session_id: UUID) -> TokenBook: ...

    async def add(self, session_id: UUID, entries: Sequence[TokenEntry]) -> bool:
        """Insert all entries, or none when another turn took a number first."""


class PiiValueVault(Protocol):
    """Short-lived shared store of values the user typed, keyed by owner, session and token."""

    async def put(self, owner_id: str, session_id: UUID, values: Sequence[TurnValue]) -> None: ...

    async def get(self, owner_id: str, session_id: UUID, label: str) -> TurnValue: ...

    async def forget(self, owner_id: str, session_id: UUID, labels: Sequence[str]) -> None: ...

    async def forget_session(self, owner_id: str, session_id: UUID) -> None: ...


class CustomerDirectory(Protocol):
    async def find(
        self, term: BlindTerm, scope: AccessScope, limit: int
    ) -> tuple[CustomerMatch, ...]:
        """Match a hidden value on raw customer columns; return masked identities only."""


class PiiLookupLog(Protocol):
    async def record(
        self,
        user_id: str,
        session_id: UUID,
        label: str,
        purpose: LookupPurpose,
        matches: int,
    ) -> None: ...

    async def count_in_session(self, session_id: UUID) -> int: ...

    async def count_for_user_since(self, user_id: str, since: datetime) -> int: ...
