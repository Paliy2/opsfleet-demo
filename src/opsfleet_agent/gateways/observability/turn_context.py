from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from opsfleet_agent.entities.prompts import PromptVersion


@dataclass
class TurnRecord:
    prompts: dict[str, str] = field(default_factory=dict)
    error_codes: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    stages: Counter[str] = field(default_factory=Counter)

    def metadata(self) -> dict[str, object]:
        return {
            "prompts": self.prompts,
            "error_codes": self.error_codes,
            "fallbacks": self.fallbacks,
            "stages": dict(self.stages),
        }


_current: ContextVar[TurnRecord | None] = ContextVar("opsfleet_turn", default=None)


@contextmanager
def recording_turn() -> Iterator[TurnRecord]:
    record = TurnRecord()
    token = _current.set(record)
    try:
        yield record
    finally:
        _current.reset(token)


def note_prompt(version: PromptVersion) -> None:
    record = _current.get()
    if record is not None:
        record.prompts[version.name] = version.tag


def note_fallback(requested: str, served: str) -> None:
    record = _current.get()
    if record is not None:
        record.fallbacks.append(f"{requested} -> {served}")
