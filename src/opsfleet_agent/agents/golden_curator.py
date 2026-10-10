import asyncio
from collections.abc import Iterable, Mapping

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.agents.prompting import render
from opsfleet_agent.core.errors import EmptyResultError, MaskedValueError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.entities.golden import GoldenTrio
from opsfleet_agent.entities.prompts import PromptName
from opsfleet_agent.entities.query import QueryResult
from opsfleet_agent.gateways.llm.chat_models import llm_errors
from opsfleet_agent.interactors.result_preview import render_preview
from opsfleet_agent.interactors.run_query import RECOVERABLE_QUERY_ERRORS, RunQuery
from opsfleet_agent.ports.agent import ProgressSink
from opsfleet_agent.ports.prompts import PromptSource

CURATOR_SCOPE = AccessScope(user_id="golden-curator", role=Role.CEO, brands=frozenset())

GOLDEN_CATEGORIES: Mapping[str, str] = {
    "revenue-trends": "revenue, orders and units over time: monthly and quarterly trends, growth",
    "brand-performance": "comparing brands: revenue, share, growth and rankings",
    "product-category": "product categories, departments and top products by revenue and units",
    "margin": "gross margin from products.cost, margin by brand and category",
    "returns-cancellations": "return and cancellation rates by brand, category and period",
    "customer-segments": "customers by age band, gender and traffic source; repeat purchase",
    "customer-lifecycle": "new vs returning, active vs churned customers, top customers by spend",
    "geography": "revenue and customers by country and state with country normalization",
    "basket-aov": "average order value, items per order, underspending customers",
    "fulfillment": "shipping and delivery times, order status mix, delayed orders",
}


class CandidateTrio(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{3,60}$")
    question: str
    approach: str
    sql: str


class CandidateBatch(BaseModel):
    trios: list[CandidateTrio]


class Rejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    reason: str


class CurationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    kept: tuple[GoldenTrio, ...]
    rejected: tuple[Rejection, ...]


class GoldenCurator:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        prompts: PromptSource,
        run_query: RunQuery,
        schema_text: str,
        preview_rows: int,
        concurrency: int = 6,
    ) -> None:
        self._proposer = model.with_structured_output(CandidateBatch, method="function_calling")
        self._model = model
        self._prompts = prompts
        self._run_query = run_query
        self._schema_text = schema_text
        self._preview_rows = preview_rows
        self._limit = asyncio.Semaphore(concurrency)

    async def curate(
        self, categories: Mapping[str, str], per_category: int, progress: ProgressSink
    ) -> CurationResult:
        batches = await asyncio.gather(
            *(
                self._propose(name, theme, per_category, progress)
                for name, theme in categories.items()
            )
        )
        outcomes = await asyncio.gather(
            *(
                self._validate(name, candidate, progress)
                for name, batch in zip(categories, batches, strict=True)
                for candidate in batch
            )
        )
        return CurationResult(
            kept=_unique(o for o in outcomes if isinstance(o, GoldenTrio)),
            rejected=tuple(o for o in outcomes if isinstance(o, Rejection)),
        )

    async def _propose(
        self, category: str, theme: str, count: int, progress: ProgressSink
    ) -> list[CandidateTrio]:
        curator, glossary = await asyncio.gather(
            self._prompts.get(PromptName.GOLDEN_CURATOR), self._prompts.get(PromptName.GLOSSARY)
        )
        system = render(
            curator.text,
            count=str(count),
            category=f"{category} — {theme}",
            schema=self._schema_text,
            glossary=glossary.text,
        )
        async with self._limit:
            with llm_errors():
                raw = await self._proposer.ainvoke(
                    [SystemMessage(system), HumanMessage(f"Write {count} trios for {category}.")]
                )
        batch = CandidateBatch.model_validate(raw)
        progress.emit(ProgressEvent(stage="proposed", detail=f"{category}: {len(batch.trios)}"))
        return batch.trios

    async def _validate(
        self, category: str, candidate: CandidateTrio, progress: ProgressSink
    ) -> GoldenTrio | Rejection:
        trio_id = f"{category}--{candidate.slug}"[:80]
        try:
            result = await self._run_query(candidate.sql, CURATOR_SCOPE)
        except (EmptyResultError, MaskedValueError, *RECOVERABLE_QUERY_ERRORS) as error:
            progress.emit(ProgressEvent(stage="rejected", detail=trio_id, code=error.code))
            return Rejection(id=trio_id, reason=f"{error.code}: {error.detail[:200]}")
        report = await self._report(candidate, result)
        progress.emit(ProgressEvent(stage="kept", detail=trio_id))
        return GoldenTrio(
            id=trio_id,
            category=category,
            question=candidate.question,
            approach=candidate.approach,
            sql=candidate.sql,
            report=report,
        )

    async def _report(self, candidate: CandidateTrio, result: QueryResult) -> str:
        reporter = await self._prompts.get(PromptName.GOLDEN_REPORTER)
        preview = render_preview(None, result, self._preview_rows)
        brief = f"Question: {candidate.question}\nApproach: {candidate.approach}\n\n{preview}"
        async with self._limit:
            with llm_errors():
                reply = await self._model.ainvoke(
                    [SystemMessage(reporter.text), HumanMessage(brief)]
                )
        return str(reply.content).strip()


def _unique(trios: Iterable[GoldenTrio]) -> tuple[GoldenTrio, ...]:
    seen: dict[str, GoldenTrio] = {}
    for trio in trios:
        seen.setdefault(trio.id, trio)
    return tuple(seen.values())
