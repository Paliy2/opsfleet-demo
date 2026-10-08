from enum import StrEnum


class AgentRole(StrEnum):
    GUARD = "guard"
    ORCHESTRATOR = "orchestrator"
    ANALYST = "analyst"
    WRITER = "writer"
    JUDGE = "judge"
    SUMMARIZER = "summarizer"
    CURATOR = "curator"


class ReasoningEffort(StrEnum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
