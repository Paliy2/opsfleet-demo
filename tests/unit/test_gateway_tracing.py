import json
from collections.abc import Iterator
from uuid import uuid4

import httpx2
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from opsfleet_agent.agents.tool_loop import ToolLoop
from opsfleet_agent.entities.telemetry import TurnKind
from opsfleet_agent.gateways.llm.trace_propagation import traced_http_client
from opsfleet_agent.gateways.observability.telemetry import TracingTelemetry

TRACEPARENT = "traceparent"
SAMPLED = 0x01


class FakeGateway:
    def __init__(self, replies: list[dict[str, object]]) -> None:
        self._replies = replies
        self.traceparents: list[str | None] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.traceparents.append(request.headers.get(TRACEPARENT))
        message = self._replies[len(self.traceparents) - 1]
        return httpx2.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "gemini-test",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        )


def answer(text: str) -> dict[str, object]:
    return {"role": "assistant", "content": text}


def echo_call() -> dict[str, object]:
    call = {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    return {"role": "assistant", "content": "", "tool_calls": [call]}


def chat_model(gateway: FakeGateway) -> ChatOpenAI:
    return ChatOpenAI(
        model="gemini-test",
        base_url="http://gateway.test/v1",
        api_key=SecretStr("vk"),
        max_retries=0,
        http_async_client=traced_http_client(httpx2.MockTransport(gateway)),
    )


async def echo() -> str:
    return "echoed"


ECHO = StructuredTool.from_function(coroutine=echo, name="echo", description="Echo.")


@pytest.fixture(scope="module")
def module_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture(scope="module")
def langfuse(module_exporter: InMemorySpanExporter) -> Iterator[Langfuse]:
    client = Langfuse(
        public_key="pk-lf-gateway-tracing-test",
        secret_key="sk-lf-test",
        host="http://langfuse.test",
        span_exporter=module_exporter,
    )
    yield client
    client.shutdown()


@pytest.fixture
def exporter(langfuse: Langfuse, module_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    langfuse.flush()
    module_exporter.clear()
    return module_exporter


def telemetry(client: Langfuse) -> TracingTelemetry:
    return TracingTelemetry(
        client, salt="test-salt-1234567", environment="test", turn_token_budget=10_000
    )


def finished(client: Langfuse, exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    client.flush()
    return list(exporter.get_finished_spans())


def span_id(span: ReadableSpan) -> str:
    assert span.context is not None
    return format(span.context.span_id, "016x")


def parent_of(traceparent: str | None) -> tuple[str, str]:
    assert traceparent is not None
    version, trace_id, parent_id, flags = traceparent.split("-")
    assert version == "00"
    assert int(flags, 16) & SAMPLED
    return trace_id, parent_id


async def test_each_llm_request_carries_its_agent_node_as_parent(
    langfuse: Langfuse, exporter: InMemorySpanExporter
) -> None:
    gateway = FakeGateway([echo_call(), answer("done")])
    tracing = telemetry(langfuse)
    loop = ToolLoop(
        chat_model(gateway), [ECHO], max_steps=4, name="analyst", callbacks=tracing.callbacks()
    )

    with tracing.turn(TurnKind.QUESTION, "mgr_a", uuid4(), "q") as turn:
        await loop.run([HumanMessage("go")])

    spans = finished(langfuse, exporter)
    by_id = {span_id(span): span for span in spans}
    parents = [parent_of(header) for header in gateway.traceparents]
    assert len(parents) == 2
    for trace_id, parent_id in parents:
        assert trace_id == turn.trace_id
        assert by_id[parent_id].name == "model"
        grandparent = by_id[parent_id].parent
        assert grandparent is not None
        assert by_id[format(grandparent.span_id, "016x")].name == "analyst"
    assert parents[0][1] != parents[1][1]


async def test_app_records_agent_and_tool_spans_but_no_generations(
    langfuse: Langfuse, exporter: InMemorySpanExporter
) -> None:
    gateway = FakeGateway([echo_call(), answer("done")])
    tracing = telemetry(langfuse)
    loop = ToolLoop(
        chat_model(gateway), [ECHO], max_steps=4, name="analyst", callbacks=tracing.callbacks()
    )

    with tracing.turn(TurnKind.QUESTION, "mgr_a", uuid4(), "q"):
        await loop.run([HumanMessage("go")])

    types = {
        (span.name, json.dumps((span.attributes or {}).get("langfuse.observation.type")))
        for span in finished(langfuse, exporter)
    }
    assert ("turn.question", '"agent"') in types
    assert ("echo", '"tool"') in types
    assert not any(kind == '"generation"' for _, kind in types)


async def test_call_outside_any_agent_run_nests_under_the_turn(
    langfuse: Langfuse, exporter: InMemorySpanExporter
) -> None:
    gateway = FakeGateway([answer("summary")])

    with telemetry(langfuse).turn(TurnKind.QUESTION, "mgr_a", uuid4(), "q") as turn:
        await chat_model(gateway).ainvoke("summarize")

    root = next(s for s in finished(langfuse, exporter) if s.name == "turn.question")
    assert parent_of(gateway.traceparents[0]) == (turn.trace_id, span_id(root))


async def test_call_without_tracing_sends_no_traceparent() -> None:
    gateway = FakeGateway([answer("hi")])

    await chat_model(gateway).ainvoke("hi")

    assert gateway.traceparents == [None]
