from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from langchain_core.tools import BaseTool

from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.context import AnalysisStep, UserPreferences
from opsfleet_agent.ports.agent import ProgressSink


@dataclass
class TurnContext:
    turn_id: UUID
    scope: AccessScope
    session_id: UUID
    progress: ProgressSink
    preferences: UserPreferences = field(default_factory=UserPreferences)
    llm_calls: int = 0
    result_ids: list[UUID] = field(default_factory=list)
    analysis: list[AnalysisStep] = field(default_factory=list)
    report_ids: list[UUID] = field(default_factory=list)
    chart_paths: list[str] = field(default_factory=list)
    chart_ids: list[UUID] = field(default_factory=list)

    @property
    def thread_id(self) -> str:
        return f"{self.session_id}:{self.turn_id}"


class Capability(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def prompt_hint(self) -> str: ...

    def tools(self, turn: TurnContext) -> list[BaseTool]: ...
