from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from opsfleet_agent.agents.tool_loop import ToolLoop
from tests.fakes import ScriptedChatModel, tool_call


async def echo(text: str) -> str:
    return f"echo:{text}"


ECHO = StructuredTool.from_function(coroutine=echo, name="echo", description="Echo text.")


async def test_runs_tools_until_model_answers() -> None:
    model = ScriptedChatModel(
        script=[tool_call("echo", "c1", text="hi"), AIMessage(content="done")]
    )

    outcome = await ToolLoop(model, [ECHO], max_steps=5).run([HumanMessage("go")])

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

    outcome = await ToolLoop(model, [ECHO], max_steps=2).run([HumanMessage("go")])

    assert outcome.text == "best effort answer"
    assert outcome.budget_exhausted
    assert "Step budget exhausted" in str(model.seen[-1][-1].content)


async def test_unknown_tool_is_reported_back_to_model() -> None:
    model = ScriptedChatModel(script=[tool_call("missing", "c1"), AIMessage(content="recovered")])

    await ToolLoop(model, [ECHO], max_steps=5).run([HumanMessage("go")])

    tool_messages = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert "unknown tool 'missing'" in str(tool_messages[0].content)
