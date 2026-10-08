from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from opsfleet_agent.agents.prompts import load_prompt
from opsfleet_agent.agents.scope_context import describe_scope
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.reports import Chart
from opsfleet_agent.gateways.llm.chat_models import llm_errors


class DraftReport(BaseModel):
    title: str = Field(description="Report title, at most 10 words.")
    body: str = Field(description="Markdown report body with the required sections.")


class ReportWriter:
    def __init__(self, model: BaseChatModel) -> None:
        self._drafter = model.with_structured_output(DraftReport, method="function_calling")

    async def write(
        self, topic: str, findings: str, charts: Sequence[Chart], scope: AccessScope
    ) -> DraftReport:
        system = load_prompt("writer").format(persona=load_prompt("persona"))
        chart_list = "\n".join(f"- {chart.title}" for chart in charts) or "none"
        brief = f"# Topic\n{topic}\n\n# Analyst findings\n{findings}\n\n# Charts\n{chart_list}"
        with llm_errors():
            raw = await self._drafter.ainvoke(
                [SystemMessage(f"{system}\n\n{describe_scope(scope)}"), HumanMessage(brief)]
            )
        return DraftReport.model_validate(raw)
