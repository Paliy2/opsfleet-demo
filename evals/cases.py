from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.core.errors import ErrorCode


class Suite(StrEnum):
    SQL = "sql"
    REDTEAM = "redteam"
    CAPABILITY = "capability"


class Attack(StrEnum):
    PII = "pii"
    INJECTION = "injection"
    SCOPE = "scope"
    DELETION = "deletion"
    OFF_TOPIC = "off_topic"
    SQL_WRITE = "sql_write"
    EXFILTRATION = "exfiltration"


class Check(StrEnum):
    NO_PII = "no_pii"
    NO_FOREIGN_BRANDS = "no_foreign_brands"
    NO_UNCONFIRMED_DELETION = "no_unconfirmed_deletion"
    NO_FOREIGN_DELETION = "no_foreign_deletion"
    NO_PROMPT_LEAK = "no_prompt_leak"
    READ_ONLY = "read_only"
    REFUSED = "refused"


class SeedReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner: str
    title: str


class RedTeamCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    attack: Attack
    user: str
    turns: tuple[str, ...] = Field(min_length=1)
    blocked_codes: frozenset[ErrorCode] = frozenset()
    checks: tuple[Check, ...] = Field(min_length=1)
    seed_reports: tuple[SeedReport, ...] = ()


class Capability(StrEnum):
    CUSTOMER_BEHAVIOR = "customer_behavior"
    PRODUCT_PERFORMANCE = "product_performance"
    TIME_METRICS = "time_metrics"
    SCHEMA = "schema"
    MULTI_STEP = "multi_step"
    REPORT = "report"
    FOLLOW_UP = "follow_up"


class Expectations(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    must_query: bool = True
    must_chart: bool = False
    must_save_report: bool = False
    max_llm_calls: int = Field(default=12, gt=0)


class CapabilityCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    category: Capability
    user: str
    turns: tuple[str, ...] = Field(min_length=1, max_length=3)
    rubric: tuple[str, ...] = Field(min_length=1)
    expectations: Expectations = Expectations()


class Grade(StrEnum):
    MET = "met"
    PARTIAL = "partial"
    UNMET = "unmet"

    @property
    def score(self) -> float:
        return {Grade.MET: 1.0, Grade.PARTIAL: 0.5, Grade.UNMET: 0.0}[self]


class TurnStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    seconds: float
    llm_calls: int
    queries: int
    retries: int
    examples: int


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: Suite
    case_id: str
    category: str
    passed: bool
    scores: dict[str, float]
    notes: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    stats: TurnStats | None = None
    error_code: str | None = None
