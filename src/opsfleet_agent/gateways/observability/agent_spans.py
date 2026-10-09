from typing import Any, override
from uuid import UUID

from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from langfuse.langchain import CallbackHandler
from opentelemetry.trace import SpanContext, TraceFlags


class AgentSpanHandler(CallbackHandler):
    def parent_span(self, run_id: UUID) -> SpanContext | None:
        observation = self._runs.get(run_id)
        if observation is None:
            return None
        return SpanContext(
            trace_id=int(observation.trace_id, 16),
            span_id=int(observation.id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )

    @override
    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        pass

    @override
    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        pass

    @override
    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        pass

    @override
    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        pass
