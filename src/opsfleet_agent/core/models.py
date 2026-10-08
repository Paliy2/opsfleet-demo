from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from opsfleet_agent.core.config import ModelProfile


class AgentRole(StrEnum):
    GUARD = "guard"
    ORCHESTRATOR = "orchestrator"
    ANALYST = "analyst"
    WRITER = "writer"
    JUDGE = "judge"
    SUMMARIZER = "summarizer"
    CURATOR = "curator"


EMBEDDING_DIMENSIONS = 768


class ModelChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    primary: str
    fallbacks: tuple[str, ...] = ()


_FLASH = ModelChoice(
    primary="gemini/gemini-3.8-flash",
    fallbacks=("gemini/gemini-3.6-flash", "gemini/gemini-3.5-flash-lite"),
)
_LITE = ModelChoice(primary="gemini/gemini-3.5-flash-lite", fallbacks=("gemini/gemini-3.6-flash",))
_PRO = ModelChoice(
    primary="gemini/gemini-3.1-pro-preview",
    fallbacks=("gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash-lite"),
)

ROSTERS: dict[ModelProfile, dict[AgentRole, ModelChoice]] = {
    ModelProfile.FREE_TIER: {
        AgentRole.GUARD: _LITE,
        AgentRole.ORCHESTRATOR: _FLASH,
        AgentRole.ANALYST: _FLASH,
        AgentRole.WRITER: _FLASH,
        AgentRole.JUDGE: _FLASH,
        AgentRole.SUMMARIZER: _LITE,
        AgentRole.CURATOR: _FLASH,
    },
    ModelProfile.PRODUCTION: {
        AgentRole.GUARD: _LITE,
        AgentRole.ORCHESTRATOR: _FLASH,
        AgentRole.ANALYST: _PRO,
        AgentRole.WRITER: _PRO,
        AgentRole.JUDGE: _PRO,
        AgentRole.SUMMARIZER: _LITE,
        AgentRole.CURATOR: _PRO,
    },
}
