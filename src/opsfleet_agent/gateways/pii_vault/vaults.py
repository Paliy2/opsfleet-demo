from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

from opsfleet_agent.core.errors import PiiValueExpiredError
from opsfleet_agent.core.identity import user_hash
from opsfleet_agent.entities.pii_tokens import TurnValue
from opsfleet_agent.gateways.pii_vault.sealer import ValueSealer
from opsfleet_agent.ports.pii_token_vault import PiiValueVault


class HashStore(Protocol):
    async def put(self, key: str, field: str, value: bytes, ttl_seconds: int) -> None: ...

    async def get(self, key: str, field: str) -> bytes | None: ...

    async def remove(self, key: str, fields: Sequence[str]) -> None: ...

    async def drop(self, key: str) -> None: ...


class RedisHashStore(HashStore):
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def put(self, key: str, field: str, value: bytes, ttl_seconds: int) -> None:
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.hset(key, field, value)
            pipe.expire(key, ttl_seconds)
            await pipe.execute()

    async def get(self, key: str, field: str) -> bytes | None:
        value = await self._client.hget(key, field)
        return value.encode() if isinstance(value, str) else value

    async def remove(self, key: str, fields: Sequence[str]) -> None:
        if fields:
            await self._client.hdel(key, *fields)

    async def drop(self, key: str) -> None:
        await self._client.delete(key)


class SealedPiiValueVault(PiiValueVault):
    def __init__(
        self, store: HashStore, sealer: ValueSealer, *, owner_salt: str, ttl: timedelta
    ) -> None:
        self._store = store
        self._sealer = sealer
        self._salt = owner_salt
        self._ttl_seconds = int(ttl.total_seconds())

    def _key(self, owner_id: str, session_id: UUID) -> str:
        return f"pii:values:{user_hash(owner_id, self._salt)}:{session_id}"

    async def put(self, owner_id: str, session_id: UUID, values: Sequence[TurnValue]) -> None:
        key = self._key(owner_id, session_id)
        for value in values:
            sealed = self._sealer.seal(f"{key}:{value.label}", value)
            await self._store.put(key, value.label, sealed, self._ttl_seconds)

    async def get(self, owner_id: str, session_id: UUID, label: str) -> TurnValue:
        key = self._key(owner_id, session_id)
        sealed = await self._store.get(key, label)
        if sealed is None:
            raise PiiValueExpiredError(f"{label} is not held for this conversation")
        return self._sealer.open(f"{key}:{label}", sealed)

    async def forget(self, owner_id: str, session_id: UUID, labels: Sequence[str]) -> None:
        await self._store.remove(self._key(owner_id, session_id), labels)

    async def forget_session(self, owner_id: str, session_id: UUID) -> None:
        await self._store.drop(self._key(owner_id, session_id))


class InProcessHashStore(HashStore):
    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self._hashes: dict[str, tuple[datetime, dict[str, bytes]]] = {}

    async def put(self, key: str, field: str, value: bytes, ttl_seconds: int) -> None:
        fields = self._live(key)
        fields[field] = value
        self._hashes[key] = (self._now() + timedelta(seconds=ttl_seconds), fields)

    async def get(self, key: str, field: str) -> bytes | None:
        return self._live(key).get(field)

    async def remove(self, key: str, fields: Sequence[str]) -> None:
        live = self._live(key)
        for field in fields:
            live.pop(field, None)

    async def drop(self, key: str) -> None:
        self._hashes.pop(key, None)

    def evict_all(self) -> None:
        self._hashes.clear()

    def _live(self, key: str) -> dict[str, bytes]:
        expires_at, fields = self._hashes.get(key, (self._now(), {}))
        if expires_at <= self._now():
            self._hashes.pop(key, None)
            return {}
        return fields
