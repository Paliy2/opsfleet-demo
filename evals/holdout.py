from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from opsfleet_agent.entities.golden import GoldenMatch, GoldenTrio
from opsfleet_agent.ports.golden import GoldenRepo

_EXCLUDED: ContextVar[frozenset[str]] = ContextVar("holdout_excluded", default=frozenset())


@contextmanager
def holding_out(trio_ids: Sequence[str]) -> Iterator[None]:
    token = _EXCLUDED.set(frozenset(trio_ids))
    try:
        yield
    finally:
        _EXCLUDED.reset(token)


class HoldoutGoldenRepo(GoldenRepo):
    def __init__(self, inner: GoldenRepo) -> None:
        self._inner = inner

    async def upsert(self, trio: GoldenTrio, embedding: Sequence[float]) -> None:
        await self._inner.upsert(trio, embedding)

    async def search(
        self, query: str, embedding: Sequence[float], limit: int, min_similarity: float
    ) -> tuple[GoldenMatch, ...]:
        excluded = _EXCLUDED.get()
        matches = await self._inner.search(query, embedding, limit + len(excluded), min_similarity)
        return tuple(match for match in matches if match.trio.id not in excluded)[:limit]
