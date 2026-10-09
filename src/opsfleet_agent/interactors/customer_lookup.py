from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.core.errors import MaskedSearchError, PiiLookupDeniedError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.pii_tokens import (
    BlindOperator,
    BlindTerm,
    LookupPurpose,
    TokenResolution,
    TurnValue,
)
from opsfleet_agent.ports.pii_token_vault import CustomerDirectory, PiiLookupLog, PiiValueVault

SEARCHABLE_TYPES = frozenset({"EMAIL_ADDRESS", "PERSON_NAME"})
_DAY = timedelta(days=1)


class LookupPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed_roles: frozenset[Role] = frozenset(Role)
    max_per_conversation: int = Field(default=10, ge=1)
    max_per_day: int = Field(default=50, ge=1)
    max_matches: int = Field(default=25, ge=1, le=25)


def exact_term(value: TurnValue) -> BlindTerm:
    return BlindTerm(
        operator=BlindOperator.EXACT,
        info_type=value.info_type,
        label=value.label,
        values=(value.value,),
    )


class ResolveCustomerTokens:
    def __init__(
        self,
        directory: CustomerDirectory,
        log: PiiLookupLog,
        values: PiiValueVault,
        policy: LookupPolicy,
        now: Callable[[], datetime],
    ) -> None:
        self._directory = directory
        self._log = log
        self._values = values
        self._policy = policy
        self._now = now

    async def __call__(
        self, scope: AccessScope, session_id: UUID, label: str, purpose: LookupPurpose
    ) -> TokenResolution:
        if scope.role not in self._policy.allowed_roles:
            raise PiiLookupDeniedError(f"role {scope.role.value} may not look up customers")
        value = await self._values.get(scope.user_id, session_id, label)
        if value.info_type not in SEARCHABLE_TYPES:
            raise MaskedSearchError(f"{label} is a {value.info_type}, which reports never hold")
        await self._within_caps(scope.user_id, session_id)
        customers = await self._directory.find(exact_term(value), scope, self._policy.max_matches)
        await self._log.record(scope.user_id, session_id, label, purpose, len(customers))
        return TokenResolution(label=label, info_type=value.info_type, customers=customers)

    async def forget(self, scope: AccessScope, session_id: UUID, labels: Sequence[str]) -> None:
        await self._values.forget(scope.user_id, session_id, labels)

    async def _within_caps(self, user_id: str, session_id: UUID) -> None:
        if await self._log.count_in_session(session_id) >= self._policy.max_per_conversation:
            raise PiiLookupDeniedError("customer lookup limit for this conversation reached")
        since = self._now() - _DAY
        if await self._log.count_for_user_since(user_id, since) >= self._policy.max_per_day:
            raise PiiLookupDeniedError("daily customer lookup limit reached")
