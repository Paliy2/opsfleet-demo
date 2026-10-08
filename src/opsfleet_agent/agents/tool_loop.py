import asyncio
from collections.abc import Sequence
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict

from opsfleet_agent.gateways.llm.chat_models import llm_errors

_BUDGET_EXHAUSTED = (
    "Step budget exhausted. Do not call tools. Answer now with what you have established, "
    "and say clearly what remains unknown."
)


class LoopState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    llm_calls: int


class LoopOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    llm_calls: int
    budget_exhausted: bool


def _text_of(message: AnyMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        part if isinstance(part, str) else str(part.get("text", ""))
        for part in content
        if isinstance(part, str) or part.get("type") == "text"
    )


def _pending_calls(state: LoopState) -> list[ToolCall]:
    last = state["messages"][-1]
    return last.tool_calls if isinstance(last, AIMessage) else []


class ToolLoop:
    def __init__(self, model: BaseChatModel, tools: Sequence[BaseTool], max_steps: int) -> None:
        self._model = model
        self._bound = model.bind_tools(tools)
        self._tools = {tool.name: tool for tool in tools}
        self._max_steps = max_steps
        graph = StateGraph(LoopState)
        graph.add_node("model", self._call_model)
        graph.add_node("tools", self._call_tools)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", self._route, ["tools", "finalize", END])
        graph.add_edge("tools", "model")
        graph.add_edge("finalize", END)
        self._graph = graph.compile()

    async def run(self, messages: Sequence[AnyMessage]) -> LoopOutcome:
        state = await self._graph.ainvoke(
            {"messages": list(messages), "llm_calls": 0},
            config={"recursion_limit": self._max_steps * 2 + 4},
        )
        final = state["messages"][-1]
        return LoopOutcome(
            text=_text_of(final),
            llm_calls=state["llm_calls"],
            budget_exhausted=state["llm_calls"] > self._max_steps,
        )

    async def _call_model(self, state: LoopState) -> dict[str, object]:
        with llm_errors():
            reply = await self._bound.ainvoke(state["messages"])
        return {"messages": [reply], "llm_calls": state["llm_calls"] + 1}

    def _route(self, state: LoopState) -> str:
        if not _pending_calls(state):
            return END
        if state["llm_calls"] >= self._max_steps:
            return "finalize"
        return "tools"

    async def _call_tools(self, state: LoopState) -> dict[str, object]:
        calls = _pending_calls(state)
        results = await asyncio.gather(*(self._invoke(call) for call in calls))
        return {"messages": list(results)}

    async def _invoke(self, call: ToolCall) -> ToolMessage:
        call_id = call["id"] or call["name"]
        tool = self._tools.get(call["name"])
        if tool is None:
            content = f"ERROR: unknown tool '{call['name']}'. Available: {sorted(self._tools)}"
            return ToolMessage(content=content, tool_call_id=call_id, name=call["name"])
        output = await tool.ainvoke(call["args"])
        return ToolMessage(content=str(output), tool_call_id=call_id, name=call["name"])

    async def _finalize(self, state: LoopState) -> dict[str, object]:
        skipped = [
            ToolMessage(
                content="SKIPPED: step budget exhausted.", tool_call_id=c["id"] or c["name"]
            )
            for c in _pending_calls(state)
        ]
        with llm_errors():
            reply = await self._model.ainvoke(
                [*state["messages"], *skipped, HumanMessage(content=_BUDGET_EXHAUSTED)]
            )
        return {"messages": [*skipped, reply], "llm_calls": state["llm_calls"] + 1}
