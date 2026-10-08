import asyncio
from collections.abc import Collection, Sequence
from typing import Annotated, Any, TypedDict

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from opsfleet_agent.gateways.llm.chat_models import llm_errors
from opsfleet_agent.gateways.llm.token_budget import turn_tokens_exhausted

_FINAL_ANSWER = (
    "Do not call tools. Answer now with what you have established, "
    "and say clearly what remains unknown."
)


class LoopState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    llm_calls: int
    finalized: bool


class LoopOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    llm_calls: int
    budget_exhausted: bool
    interrupt: dict[str, Any] | None = None


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


def _after_tools(_state: LoopState) -> str:
    return "finalize" if turn_tokens_exhausted() else "model"


def _call_id(call: ToolCall) -> str:
    return call["id"] or call["name"]


class ToolLoop:
    def __init__(
        self,
        model: BaseChatModel,
        tools: Sequence[BaseTool],
        max_steps: int,
        *,
        name: str,
        exclusive_tools: Collection[str] = (),
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> None:
        self._name = name
        self._callbacks = list(callbacks)
        self._model = model
        self._bound = model.bind_tools(tools)
        self._tools = {tool.name: tool for tool in tools}
        self._exclusive = frozenset(exclusive_tools)
        self._max_steps = max_steps
        graph = StateGraph(LoopState)
        graph.add_node("model", self._call_model)
        graph.add_node("tools", self._call_tools)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", self._route, ["tools", "finalize", END])
        graph.add_conditional_edges("tools", _after_tools, ["model", "finalize"])
        graph.add_edge("finalize", END)
        self._graph = graph.compile(checkpointer=checkpointer)

    async def run(
        self, messages: Sequence[AnyMessage], thread_id: str | None = None
    ) -> LoopOutcome:
        state = await self._graph.ainvoke(
            {"messages": list(messages), "llm_calls": 0, "finalized": False},
            config=self._config(thread_id),
        )
        return self._outcome(state)

    async def resume(self, thread_id: str, value: dict[str, Any]) -> LoopOutcome:
        state = await self._graph.ainvoke(Command(resume=value), config=self._config(thread_id))
        return self._outcome(state)

    def _config(self, thread_id: str | None) -> RunnableConfig:
        config: RunnableConfig = {
            "recursion_limit": self._max_steps * 2 + 4,
            "run_name": self._name,
        }
        if self._callbacks:
            config["callbacks"] = self._callbacks
        if thread_id is not None:
            config["configurable"] = {"thread_id": thread_id}
        return config

    def _outcome(self, state: dict[str, Any]) -> LoopOutcome:
        interrupts = state.get("__interrupt__") or ()
        return LoopOutcome(
            text="" if interrupts else _text_of(state["messages"][-1]),
            llm_calls=state["llm_calls"],
            budget_exhausted=state["finalized"],
            interrupt=interrupts[0].value if interrupts else None,
        )

    async def _call_model(self, state: LoopState) -> dict[str, object]:
        with llm_errors():
            reply = await self._bound.ainvoke(state["messages"])
        return {"messages": [reply], "llm_calls": state["llm_calls"] + 1}

    def _route(self, state: LoopState) -> str:
        if not _pending_calls(state):
            return END
        if state["llm_calls"] >= self._max_steps or turn_tokens_exhausted():
            return "finalize"
        return "tools"

    async def _call_tools(self, state: LoopState) -> dict[str, object]:
        calls = _pending_calls(state)
        crowded = len(calls) > 1
        results = await asyncio.gather(*(self._invoke(call, crowded=crowded) for call in calls))
        return {"messages": list(results)}

    async def _invoke(self, call: ToolCall, *, crowded: bool) -> ToolMessage:
        tool = self._tools.get(call["name"])
        if tool is None:
            content = f"ERROR: unknown tool '{call['name']}'. Available: {sorted(self._tools)}"
        elif crowded and call["name"] in self._exclusive:
            content = f"ERROR: '{call['name']}' must be called alone in its own step. Retry alone."
        else:
            content = str(await tool.ainvoke(call["args"]))
        return ToolMessage(content=content, tool_call_id=_call_id(call), name=call["name"])

    async def _finalize(self, state: LoopState) -> dict[str, object]:
        budget = "Token budget" if turn_tokens_exhausted() else "Step budget"
        skipped = [
            ToolMessage(
                content=f"SKIPPED: {budget.lower()} exhausted.", tool_call_id=_call_id(call)
            )
            for call in _pending_calls(state)
        ]
        notice = HumanMessage(content=f"{budget} exhausted. {_FINAL_ANSWER}")
        with llm_errors():
            reply = await self._model.ainvoke([*state["messages"], *skipped, notice])
        return {
            "messages": [*skipped, reply],
            "llm_calls": state["llm_calls"] + 1,
            "finalized": True,
        }
