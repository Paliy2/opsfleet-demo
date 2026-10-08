from collections.abc import Sequence

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.models import EMBEDDING_DIMENSIONS
from opsfleet_agent.entities.golden import GoldenMatch, GoldenTrio
from opsfleet_agent.models.tables import GoldenTrioRow
from opsfleet_agent.ports.golden import GoldenRepo

_RRF_K = 60
_CANDIDATE_POOL = 30

_HYBRID_SEARCH = text(
    """
    WITH query AS (
        SELECT to_tsquery(
            'english', replace(plainto_tsquery('english', :text)::text, '&', '|')
        ) AS terms
    ),
    semantic AS (
        SELECT id, row_number() OVER (ORDER BY embedding <=> :embedding) AS rank
        FROM golden_trios
        ORDER BY embedding <=> :embedding
        LIMIT :pool
    ),
    lexical AS (
        SELECT id, row_number() OVER (ORDER BY ts_rank_cd(search_vector, terms) DESC) AS rank
        FROM golden_trios, query
        WHERE search_vector @@ terms
        ORDER BY ts_rank_cd(search_vector, terms) DESC
        LIMIT :pool
    ),
    fused AS (
        SELECT id,
               COALESCE(1.0 / (:rrf_k + semantic.rank), 0)
             + COALESCE(1.0 / (:rrf_k + lexical.rank), 0) AS score
        FROM semantic FULL OUTER JOIN lexical USING (id)
    )
    SELECT g.id, g.category, g.question, g.approach, g.sql, g.report,
           1 - (g.embedding <=> :embedding) AS similarity, fused.score
    FROM fused JOIN golden_trios g USING (id)
    WHERE 1 - (g.embedding <=> :embedding) >= :min_similarity
    ORDER BY fused.score DESC
    LIMIT :limit
    """
).bindparams(bindparam("embedding", type_=Vector(EMBEDDING_DIMENSIONS)))


class SqlGoldenRepo(GoldenRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert(self, trio: GoldenTrio, embedding: Sequence[float]) -> None:
        values = {**trio.model_dump(), "embedding": list(embedding)}
        statement = insert(GoldenTrioRow).values(**values)
        async with self._sessions.begin() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[GoldenTrioRow.id],
                    set_={key: statement.excluded[key] for key in values if key != "id"},
                )
            )

    async def search(
        self, query: str, embedding: Sequence[float], limit: int, min_similarity: float
    ) -> tuple[GoldenMatch, ...]:
        async with self._sessions() as session:
            rows = await session.execute(
                _HYBRID_SEARCH,
                {
                    "text": query,
                    "embedding": list(embedding),
                    "pool": _CANDIDATE_POOL,
                    "rrf_k": _RRF_K,
                    "min_similarity": min_similarity,
                    "limit": limit,
                },
            )
            return tuple(
                GoldenMatch(
                    trio=GoldenTrio(
                        id=row.id,
                        category=row.category,
                        question=row.question,
                        approach=row.approach,
                        sql=row.sql,
                        report=row.report,
                    ),
                    similarity=float(row.similarity),
                    score=float(row.score),
                )
                for row in rows
            )
