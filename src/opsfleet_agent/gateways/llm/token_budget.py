from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, override

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, LLMResult


@dataclass
class TokenMeter:
    limit: int
    used: int = 0
    cached: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


_meter: ContextVar[TokenMeter | None] = ContextVar("opsfleet_token_meter", default=None)


@contextmanager
def metered_turn(limit: int) -> Iterator[TokenMeter]:
    meter = TokenMeter(limit)
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


def turn_tokens_exhausted() -> bool:
    meter = _meter.get()
    return meter is not None and meter.exhausted


def _usages(response: LLMResult) -> list[UsageMetadata]:
    return [
        generation.message.usage_metadata
        for candidates in response.generations
        for generation in candidates
        if isinstance(generation, ChatGeneration)
        and isinstance(generation.message, AIMessage)
        and generation.message.usage_metadata
    ]


class TokenMeterCallback(BaseCallbackHandler):
    run_inline = True

    @override
    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        meter = _meter.get()
        if meter is None:
            return
        for usage in _usages(response):
            meter.used += usage["total_tokens"]
            meter.cached += usage.get("input_token_details", {}).get("cache_read") or 0
