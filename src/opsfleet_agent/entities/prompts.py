from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PromptName(StrEnum):
    ORCHESTRATOR = "agents/orchestrator/system"
    ANALYST = "agents/analyst/system"
    GLOSSARY = "agents/analyst/glossary"
    GUARD = "agents/guard/system"
    WRITER = "agents/writer/system"
    PERSONA = "agents/persona"


class PromptOrigin(StrEnum):
    LANGFUSE = "langfuse"
    LOCAL = "local"


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int | None = Field(default=None, gt=0, le=20)
    max_sql_failures: int | None = Field(default=None, gt=0, le=10)


class PromptVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: PromptName
    text: str
    origin: PromptOrigin
    version: int | None = None
    config: AgentConfig = AgentConfig()

    @property
    def tag(self) -> str:
        return f"{self.origin}:v{self.version}" if self.version else str(self.origin)


class PublishedPrompt(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: PromptName
    version: int
    changed: bool
