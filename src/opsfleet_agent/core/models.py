from pydantic import BaseModel, ConfigDict

from opsfleet_agent.core.config import ModelProfile
from opsfleet_agent.core.roles import AgentRole, ReasoningEffort

EMBEDDING_DIMENSIONS = 768


class ModelChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    primary: str
    fallbacks: tuple[str, ...] = ()
    reasoning_effort: ReasoningEffort | None = None

    def thinking(self, effort: ReasoningEffort | None) -> "ModelChoice":
        return self.model_copy(update={"reasoning_effort": effort})


_FLASH = ModelChoice(
    primary="gemini/gemini-3.8-flash",
    fallbacks=("gemini/gemini-3.6-flash", "gemini/gemini-3.5-flash-lite"),
)
_LITE = ModelChoice(primary="gemini/gemini-3.5-flash-lite", fallbacks=("gemini/gemini-3.6-flash",))
_PRO = ModelChoice(
    primary="gemini/gemini-3.1-pro-preview",
    fallbacks=("gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash-lite"),
)

_LOW = ReasoningEffort.LOW
_MEDIUM = ReasoningEffort.MEDIUM

ROSTERS: dict[ModelProfile, dict[AgentRole, ModelChoice]] = {
    ModelProfile.FREE_TIER: {
        AgentRole.GUARD: _LITE.thinking(_LOW),
        AgentRole.ORCHESTRATOR: _FLASH.thinking(_LOW),
        AgentRole.ANALYST: _FLASH.thinking(_LOW),
        AgentRole.WRITER: _FLASH.thinking(_LOW),
        AgentRole.JUDGE: _FLASH.thinking(_MEDIUM),
        AgentRole.SUMMARIZER: _LITE.thinking(_LOW),
        AgentRole.CURATOR: _FLASH.thinking(_MEDIUM),
    },
    ModelProfile.PRODUCTION: {
        AgentRole.GUARD: _LITE.thinking(_LOW),
        AgentRole.ORCHESTRATOR: _FLASH.thinking(_LOW),
        AgentRole.ANALYST: _PRO.thinking(_LOW),
        AgentRole.WRITER: _PRO.thinking(_LOW),
        AgentRole.JUDGE: _PRO.thinking(_MEDIUM),
        AgentRole.SUMMARIZER: _LITE.thinking(_LOW),
        AgentRole.CURATOR: _PRO.thinking(_MEDIUM),
    },
}
