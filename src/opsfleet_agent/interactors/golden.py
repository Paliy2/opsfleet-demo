from collections.abc import Sequence

from opsfleet_agent.entities.golden import GoldenMatch, GoldenTrio
from opsfleet_agent.ports.golden import Embedder, GoldenRepo


def render_example(trio: GoldenTrio, index: int) -> str:
    return (
        f"### Example {index} ({trio.category})\n"
        f"Question: {trio.question}\n"
        f"Approach: {trio.approach}\n"
        f"```sql\n{trio.sql.strip()}\n```"
    )


def render_examples(matches: Sequence[GoldenMatch], max_chars: int) -> str:
    blocks: list[str] = []
    budget = max_chars
    for match in matches:
        block = render_example(match.trio, len(blocks) + 1)
        if len(block) > budget:
            break
        blocks.append(block)
        budget -= len(block)
    return "\n\n".join(blocks)


class RetrieveExamples:
    def __init__(
        self, embedder: Embedder, repo: GoldenRepo, *, limit: int, min_similarity: float
    ) -> None:
        self._embedder = embedder
        self._repo = repo
        self._limit = limit
        self._min_similarity = min_similarity

    async def __call__(self, task: str) -> tuple[GoldenMatch, ...]:
        if self._limit == 0:
            return ()
        [embedding] = await self._embedder.embed([task])
        return await self._repo.search(task, embedding, self._limit, self._min_similarity)


class LoadGoldenTrios:
    def __init__(self, embedder: Embedder, repo: GoldenRepo) -> None:
        self._embedder = embedder
        self._repo = repo

    async def __call__(self, trios: Sequence[GoldenTrio]) -> int:
        embeddings = await self._embedder.embed([trio.search_text for trio in trios])
        for trio, embedding in zip(trios, embeddings, strict=True):
            await self._repo.upsert(trio, embedding)
        return len(trios)
