from dataclasses import dataclass, field
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from opsfleet_agent.agents.analyst import DataAnalyst
from opsfleet_agent.agents.prompts import load_prompt
from opsfleet_agent.agents.scope_context import describe_scope
from opsfleet_agent.agents.tool_loop import ToolLoop
from opsfleet_agent.entities.conversation import (
    AgentReply,
    AgentRequest,
    ChatMessage,
    MessageRole,
    ProgressEvent,
)
from opsfleet_agent.ports.agent import AnalysisAgent, ProgressSink


class DataAnalystArgs(BaseModel):
    task: str = Field(
        description="Self-contained analysis task: metric, filters, time range, comparison."
    )


class _NoArgs(BaseModel):
    pass


@dataclass
class _TurnTally:
    llm_calls: int = 0
    result_ids: list[UUID] = field(default_factory=list)


def _as_message(message: ChatMessage) -> AnyMessage:
    if message.role is MessageRole.USER:
        return HumanMessage(message.content)
    return AIMessage(message.content)


class Orchestrator(AnalysisAgent):
    def __init__(
        self, model: BaseChatModel, analyst: DataAnalyst, schema_text: str, max_steps: int
    ) -> None:
        self._model = model
        self._analyst = analyst
        self._schema_text = schema_text
        self._max_steps = max_steps
        self._system_prompt = load_prompt("orchestrator")

    async def respond(self, request: AgentRequest, progress: ProgressSink) -> AgentReply:
        tally = _TurnTally()
        loop = ToolLoop(self._model, self._tools(request, progress, tally), self._max_steps)
        progress.emit(ProgressEvent(stage="thinking"))
        outcome = await loop.run(
            [
                SystemMessage(f"{self._system_prompt}\n\n{describe_scope(request.scope)}"),
                *(_as_message(message) for message in request.history),
                HumanMessage(request.question),
            ]
        )
        return AgentReply(
            text=outcome.text,
            result_ids=tuple(tally.result_ids),
            llm_calls=outcome.llm_calls + tally.llm_calls,
        )

    def _tools(
        self, request: AgentRequest, progress: ProgressSink, tally: _TurnTally
    ) -> list[BaseTool]:
        async def data_analyst(task: str) -> str:
            progress.emit(ProgressEvent(stage="analysis", detail=task))
            finding = await self._analyst.analyze(task, request.scope, request.session_id, progress)
            tally.llm_calls += finding.llm_calls
            tally.result_ids.extend(finding.result_ids)
            return finding.summary

        async def describe_schema() -> str:
            progress.emit(ProgressEvent(stage="schema"))
            return self._schema_text

        return [
            StructuredTool.from_function(
                coroutine=data_analyst,
                name="data_analyst",
                description="Delegate a data question to the SQL analyst; returns findings.",
                args_schema=DataAnalystArgs,
            ),
            StructuredTool.from_function(
                coroutine=describe_schema,
                name="describe_schema",
                description="Describe available tables, columns and masking rules.",
                args_schema=_NoArgs,
            ),
        ]
