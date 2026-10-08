from collections.abc import Sequence
from typing import Protocol

from opsfleet_agent.entities.golden import GoldenMatch, GoldenTrio


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class GoldenRepo(Protocol):
    async def upsert(self, trio: GoldenTrio, embedding: Sequence[float]) -> None: ...

    async def search(
        self, query: str, embedding: Sequence[float], limit: int, min_similarity: float
    ) -> tuple[GoldenMatch, ...]:
        """Hybrid vector + full-text search, best first, only above min_similarity."""
