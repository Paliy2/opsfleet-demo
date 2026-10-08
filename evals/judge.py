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


class RefusalVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    declined: bool
    reason: str


def _tables(results: Sequence[QueryResult]) -> str:
    if not results:
        return "(the assistant ran no queries)"
    return "\n\n".join(render_preview(None, result, _PREVIEW_ROWS) for result in results)


class Judge:
    def __init__(self, model: BaseChatModel) -> None:
        self._grader = model.with_structured_output(Verdict, method="function_calling")
        self._auditor = model.with_structured_output(RefusalVerdict, method="function_calling")
        self._grading = (_PROMPTS / "judge.md").read_text(encoding="utf-8")
        self._refusal = (_PROMPTS / "refusal.md").read_text(encoding="utf-8")

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
