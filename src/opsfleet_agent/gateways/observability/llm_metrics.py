from dataclasses import dataclass
from time import perf_counter
from typing import Any, override
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from opsfleet_agent.gateways.observability import metrics
from opsfleet_agent.gateways.observability.turn_context import note_fallback


@dataclass(frozen=True)
class _PendingCall:
    requested: str
    started: float


def _model_id(name: str) -> str:
    return name.rsplit("/", 1)[-1]


class LlmMetricsCallback(BaseCallbackHandler):
    run_inline = True

    def __init__(self) -> None:
        self._pending: dict[UUID, _PendingCall] = {}

    @override
    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        params = kwargs.get("invocation_params") or {}
        requested = str(params.get("model") or params.get("model_name") or "unknown")
        self._pending[run_id] = _PendingCall(_model_id(requested), perf_counter())

    @override
    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        call = self._pending.pop(run_id, None)
        if call is None:
            return
        output = response.llm_output or {}
        served = _model_id(str(output.get("model_name") or call.requested))
        metrics.LLM_CALLS.labels(call.requested, served).inc()
        metrics.LLM_SECONDS.labels(served).observe(perf_counter() - call.started)
        usage = output.get("token_usage") or {}
        metrics.LLM_TOKENS.labels(served, "input").inc(usage.get("prompt_tokens") or 0)
        metrics.LLM_TOKENS.labels(served, "output").inc(usage.get("completion_tokens") or 0)
        if served != call.requested:
            metrics.LLM_FALLBACKS.labels(call.requested, served).inc()
            note_fallback(call.requested, served)

    @override
    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        call = self._pending.pop(run_id, None)
        requested = call.requested if call else "unknown"
        metrics.LLM_ERRORS.labels(requested, type(error).__name__).inc()
