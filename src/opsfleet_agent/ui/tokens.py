import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from opsfleet_agent.core.errors import UnauthenticatedError

SIGN_IN_HANDLE = "sign_in_handle"


@dataclass(frozen=True)
class _Held:
    owner: str
    token: str
    expires_at: float


class TokenVault:
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._held: dict[str, _Held] = {}

    def keep(self, owner: str, token: str) -> str:
        now = self._clock()
        self._held = {key: held for key, held in self._held.items() if held.expires_at > now}
        handle = secrets.token_urlsafe(32)
        self._held[handle] = _Held(owner, token, now + self._ttl)
        return handle

    def token(self, owner: str, handle: str) -> str:
        held = self._held.get(handle)
        if held is None or held.owner != owner:
            raise UnauthenticatedError("no server-side token for this sign-in")
        if held.expires_at <= self._clock():
            del self._held[handle]
            raise UnauthenticatedError("server-side token expired")
        return held.token
