from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from prometheus_client import REGISTRY

from opsfleet_agent.core.errors import ErrorCode, OffTopicError
from opsfleet_agent.entities.conversation import AgentReply, ProgressEvent
from opsfleet_agent.entities.telemetry import TurnKind
from opsfleet_agent.gateways.llm.token_budget import TokenMeterCallback, turn_tokens_exhausted
from opsfleet_agent.gateways.observability.llm_metrics import LlmMetricsCallback
from opsfleet_agent.gateways.observability.telemetry import TracingTelemetry
from opsfleet_agent.gateways.observability.turn_context import recording_turn


def telemetry() -> TracingTelemetry:
    return TracingTelemetry(
        None, salt="test-salt-1234567", environment="test", turn_token_budget=1_000
    )


def sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_agent_error_is_tagged_with_trace_id_and_counted() -> None:
    labels = {"kind": "question", "outcome": ErrorCode.OFF_TOPIC.value}
    before = sample("opsfleet_turns_total", labels)

    with (
        pytest.raises(OffTopicError) as raised,
        telemetry().turn(TurnKind.QUESTION, "mgr_a", uuid4(), "write python") as trace,
    ):
        raise OffTopicError("off topic")

    assert raised.value.trace_id == trace.trace_id
    assert sample("opsfleet_turns_total", labels) == before + 1


def test_tool_errors_from_progress_events_are_counted() -> None:
    labels = {"code": ErrorCode.SQL_SYNTAX.value}
    before = sample("opsfleet_tool_errors_total", labels)

    with telemetry().turn(TurnKind.QUESTION, "mgr_a", uuid4(), "q") as trace:
        trace.emit(ProgressEvent(stage="retry", code=ErrorCode.SQL_SYNTAX))
        trace.complete(AgentReply(text="ok", turn_id=uuid4(), llm_calls=3))

    assert sample("opsfleet_tool_errors_total", labels) == before + 1


def test_fallback_model_is_detected_and_attached_to_turn() -> None:
    callback, run_id = LlmMetricsCallback(), uuid4()
    labels = {"requested": "gemini-3.8-flash", "served": "gemini-3.6-flash"}
    before = sample("opsfleet_llm_fallbacks_total", labels)

    with recording_turn() as record:
        callback.on_chat_model_start(
            {}, [[]], run_id=run_id, invocation_params={"model": "gemini-3.8-flash"}
        )
        callback.on_llm_end(
            LLMResult(
                generations=[[ChatGeneration(message=AIMessage("hi"))]],
                llm_output={
                    "model_name": "gemini/gemini-3.6-flash",
                    "token_usage": {"prompt_tokens": 10, "completion_tokens": 2},
                },
            ),
            run_id=run_id,
        )

    assert sample("opsfleet_llm_fallbacks_total", labels) == before + 1
    assert record.fallbacks == ["gemini-3.8-flash -> gemini-3.6-flash"]


def test_turn_meters_tokens_against_its_budget() -> None:
    labels = {"kind": "question"}
    before = sample("opsfleet_turn_tokens_sum", labels)
    reply = AIMessage(
        "hi", usage_metadata={"input_tokens": 900, "output_tokens": 300, "total_tokens": 1_200}
    )

    with telemetry().turn(TurnKind.QUESTION, "mgr_a", uuid4(), "q"):
        assert not turn_tokens_exhausted()
        TokenMeterCallback().on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=reply)]]), run_id=uuid4()
        )
        assert turn_tokens_exhausted()

    assert not turn_tokens_exhausted()
    assert sample("opsfleet_turn_tokens_sum", labels) == before + 1_200
