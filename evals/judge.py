from collections.abc import Sequence
from datetime import date
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from evals.cases import Grade
from opsfleet_agent.agents.prompting import render
from opsfleet_agent.entities.query import QueryResult
from opsfleet_agent.gateways.llm.chat_models import llm_errors
from opsfleet_agent.interactors.result_preview import render_preview

_PROMPTS = Path(__file__).parent / "prompts"
_PREVIEW_ROWS = 60


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    correctness: Grade
    correctness_reason: str
    groundedness: Grade
    groundedness_reason: str
    completeness: Grade
    completeness_reason: str


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    criterion: str
    grade: Grade
    reason: str


class CapabilityVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    criteria: list[CriterionVerdict]
    intent: Grade
    intent_reason: str
    groundedness: Grade
    groundedness_reason: str
    actionability: Grade
    actionability_reason: str
    clarity: Grade
    clarity_reason: str
    overall: Grade
    overall_reason: str


class Exchange(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str
    answer: str
    charts: int = 0
    reports: int = 0


class RefusalVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    declined: bool
    reason: str


def _numbered(lines: Sequence[str]) -> str:
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))


def _conversation(exchanges: Sequence[Exchange]) -> str:
    return "\n\n".join(
        f"### User\n{exchange.question}\n\n### Assistant\n{exchange.answer}\n\n"
        f"(artifacts this turn: {exchange.charts} chart(s) rendered, "
        f"{exchange.reports} report(s) saved)"
        for exchange in exchanges
    )


def _reports(bodies: Sequence[str]) -> str:
    return "\n\n---\n\n".join(bodies) if bodies else "(no report was saved)"


def _tables(results: Sequence[QueryResult]) -> str:
    if not results:
        return "(the assistant ran no queries)"
    return "\n\n".join(render_preview(None, result, _PREVIEW_ROWS) for result in results)


class Judge:
    def __init__(self, model: BaseChatModel) -> None:
        self._grader = model.with_structured_output(Verdict, method="function_calling")
        self._auditor = model.with_structured_output(RefusalVerdict, method="function_calling")
        self._assessor = model.with_structured_output(CapabilityVerdict, method="function_calling")
        self._grading = (_PROMPTS / "judge.md").read_text(encoding="utf-8")
        self._refusal = (_PROMPTS / "refusal.md").read_text(encoding="utf-8")
        self._capability = (_PROMPTS / "capability.md").read_text(encoding="utf-8")

    async def grade(
        self,
        *,
        question: str,
        reference_answer: str,
        reference: QueryResult,
        answer: str,
        results: Sequence[QueryResult],
    ) -> Verdict:
        evidence = (
            f"## Question\n{question}\n\n## Reference answer\n{reference_answer}\n\n"
            f"## Reference result\n{render_preview(None, reference, _PREVIEW_ROWS)}\n\n"
            f"## Assistant answer\n{answer}\n\n## Assistant result tables\n{_tables(results)}"
        )
        system = render(self._grading, today=date.today().isoformat())
        with llm_errors():
            raw = await self._grader.ainvoke([SystemMessage(system), HumanMessage(evidence)])
        return Verdict.model_validate(raw)

    async def declined(self, request: str, reply: str) -> RefusalVerdict:
        evidence = f"## User request\n{request}\n\n## Assistant reply\n{reply}"
        with llm_errors():
            raw = await self._auditor.ainvoke(
                [SystemMessage(self._refusal), HumanMessage(evidence)]
            )
        return RefusalVerdict.model_validate(raw)

    async def assess(
        self,
        *,
        rubric: Sequence[str],
        report_requested: bool,
        exchanges: Sequence[Exchange],
        reports: Sequence[str],
        results: Sequence[QueryResult],
    ) -> CapabilityVerdict:
        evidence = (
            f"## Rubric\n{_numbered(rubric)}\n\n"
            f"## Report requested\n{'yes' if report_requested else 'no'}\n\n"
            f"## Conversation\n{_conversation(exchanges)}\n\n"
            f"## Saved reports\n{_reports(reports)}\n\n"
            f"## Assistant result tables\n{_tables(results)}"
        )
        system = render(self._capability, today=date.today().isoformat())
        with llm_errors():
            raw = await self._assessor.ainvoke([SystemMessage(system), HumanMessage(evidence)])
        return CapabilityVerdict.model_validate(raw)
