from typing import Protocol, Self

import anyio
import jwt
from jwt import PyJWK, PyJWKClient

from opsfleet_agent.core.errors import UnauthenticatedError
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.identity import Principal
from opsfleet_agent.ports.identity import TokenVerifier

_ALGORITHMS = ["RS256"]


class SigningKeys(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> PyJWK: ...


class JwtVerifier(TokenVerifier):
    def __init__(self, *, issuer: str, audience: str | None, keys: SigningKeys) -> None:
        self._issuer = issuer
        self._audience = audience
        self._keys = keys

    @classmethod
    def from_jwks(cls, *, issuer: str, jwks_url: str, audience: str | None) -> Self:
        keys = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        return cls(issuer=issuer, audience=audience, keys=keys)

    async def verify(self, token: str) -> Principal:
        try:
            key = await anyio.to_thread.run_sync(self._keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=_ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                options={"verify_aud": self._audience is not None, "require": ["exp", "iss"]},
            )
        except jwt.PyJWTError as error:
            raise UnauthenticatedError(f"invalid token: {error}") from error
        username = claims.get("preferred_username")
        if not isinstance(username, str) or not username:
            raise UnauthenticatedError("token has no preferred_username")
        realm_roles = claims.get("realm_access", {}).get("roles", [])
        roles = frozenset(Role(role) for role in realm_roles if role in Role.__members__.values())
        return Principal(user_id=username, roles=roles)
