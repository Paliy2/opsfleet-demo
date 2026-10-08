from typing import Protocol

from opsfleet_agent.entities.identity import Principal


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Principal: ...


class RateLimiter(Protocol):
    async def hit(self, user_id: str) -> None:
        """Count one request for the user; raise RateLimitedError when over budget."""
