from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, override
from uuid import UUID

import httpx2
import openai
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

SpanLookup = Callable[[UUID], SpanContext | None]

_llm_parent: ContextVar[SpanContext | None] = ContextVar("opsfleet_llm_parent", default=None)
_propagator = TraceContextTextMapPropagator()


def _outgoing_context() -> context.Context:
    parent = _llm_parent.get()
    if parent is None:
        return context.get_current()
    return trace.set_span_in_context(NonRecordingSpan(parent))


async def inject_traceparent(request: httpx2.Request) -> None:
    _propagator.inject(request.headers, context=_outgoing_context())


def traced_http_client(transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    return openai.DefaultAsyncHttpxClient(
        transport=transport, event_hooks={"request": [inject_traceparent]}
    )


class LlmParentSpanCallback(BaseCallbackHandler):
    run_inline = True

    def __init__(self, parent_span: SpanLookup) -> None:
        self._parent_span = parent_span

    @override
    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        _llm_parent.set(None if parent_run_id is None else self._parent_span(parent_run_id))
