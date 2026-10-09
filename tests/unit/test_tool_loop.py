import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from opsfleet_agent.agents.tool_loop import ToolLoop
from opsfleet_agent.core.errors import MaskedValueError
from opsfleet_agent.gateways.llm.token_budget import TokenMeterCallback, metered_turn
from tests.fakes import ScriptedChatModel, tool_call


async def echo(text: str) -> str:
    return f"echo:{text}"


ECHO = StructuredTool.from_function(coroutine=echo, name="echo", description="Echo text.")


async def test_runs_tools_until_model_answers() -> None:
    model = ScriptedChatModel(
        script=[tool_call("echo", "c1", text="hi"), AIMessage(content="done")]
    )

    outcome = await ToolLoop(model, [ECHO], max_steps=5, name="test").run([HumanMessage("go")])

    assert outcome.text == "done"
    assert outcome.llm_calls == 2
    assert not outcome.budget_exhausted


async def test_step_budget_forces_final_answer_without_tools() -> None:
    model = ScriptedChatModel(
        script=[
            tool_call("echo", "c1", text="1"),
            tool_call("echo", "c2", text="2"),
            AIMessage(content="best effort answer"),
        ]
    )

    outcome = await ToolLoop(model, [ECHO], max_steps=2, name="test").run([HumanMessage("go")])

    assert outcome.text == "best effort answer"
    assert outcome.budget_exhausted
    assert "Step budget exhausted" in str(model.seen[-1][-1].content)


async def test_unknown_tool_is_reported_back_to_model() -> None:
    model = ScriptedChatModel(script=[tool_call("missing", "c1"), AIMessage(content="recovered")])

    await ToolLoop(model, [ECHO], max_steps=5, name="test").run([HumanMessage("go")])

    tool_messages = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert "unknown tool 'missing'" in str(tool_messages[0].content)


def costly(message: AIMessage, tokens: int) -> AIMessage:
    message.usage_metadata = {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}
    return message


def metered(script: list[AIMessage]) -> ScriptedChatModel:
    return ScriptedChatModel(script=script, callbacks=[TokenMeterCallback()])


async def test_token_budget_forces_final_answer_without_tools() -> None:
    model = metered(
        [
            costly(tool_call("echo", "c1", text="1"), 600),
            costly(tool_call("echo", "c2", text="2"), 600),
            costly(AIMessage(content="partial answer"), 50),
        ]
    )

    with metered_turn(1_000) as meter:
        outcome = await ToolLoop(model, [ECHO], max_steps=8, name="test").run([HumanMessage("go")])

    assert outcome.text == "partial answer"
    assert outcome.budget_exhausted
    assert outcome.llm_calls == 3
    assert meter.used == 1_250
    assert "Token budget exhausted" in str(model.seen[-1][-1].content)


async def test_nested_loop_spends_the_same_turn_budget() -> None:
    inner = metered(
        [
            costly(tool_call("echo", "i1", text="x"), 900),
            costly(AIMessage(content="inner partial"), 10),
        ]
    )

    async def delegate(task: str) -> str:
        nested = ToolLoop(inner, [ECHO], max_steps=8, name="inner")
        return (await nested.run([HumanMessage(task)])).text

    outer = metered(
        [
            costly(tool_call("delegate", "o1", task="dig"), 200),
            costly(AIMessage(content="outer partial"), 10),
        ]
    )
    tools = [StructuredTool.from_function(coroutine=delegate, name="delegate", description="D.")]

    with metered_turn(1_000) as meter:
        outcome = await ToolLoop(outer, tools, max_steps=8, name="outer").run([HumanMessage("go")])

    assert outcome.text == "outer partial"
    assert outcome.budget_exhausted
    assert meter.used == 1_120
    assert "Token budget exhausted" in str(inner.seen[-1][-1].content)
    assert "Token budget exhausted" in str(outer.seen[-1][-1].content)


async def test_unmetered_loop_ignores_token_usage() -> None:
    model = metered(
        [costly(tool_call("echo", "c1", text="1"), 10**9), costly(AIMessage(content="ok"), 1)]
    )

    outcome = await ToolLoop(model, [ECHO], max_steps=5, name="test").run([HumanMessage("go")])

    assert outcome.text == "ok"
    assert not outcome.budget_exhausted


async def test_tool_error_surfaces_after_parallel_siblings_finish() -> None:
    finished: list[str] = []

    async def slow(text: str) -> str:
        await asyncio.sleep(0.01)
        finished.append(text)
        return text

    async def refuse(text: str) -> str:
        raise MaskedValueError(text)

    tools = [
        StructuredTool.from_function(coroutine=slow, name="slow", description="Slow echo."),
        StructuredTool.from_function(coroutine=refuse, name="refuse", description="Refuse."),
    ]
    both = AIMessage(
        content="",
        tool_calls=[
            {"name": "refuse", "args": {"text": "[PERSON_NAME]"}, "id": "r1"},
            {"name": "slow", "args": {"text": "sibling"}, "id": "s1"},
        ],
    )
    model = ScriptedChatModel(script=[both, AIMessage(content="unused")])

    with pytest.raises(MaskedValueError):
        await ToolLoop(model, tools, max_steps=5, name="test").run([HumanMessage("go")])

    assert finished == ["sibling"]
    assert len(model.seen) == 1
