from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from opsfleet_agent.core.roles import AgentRole, ReasoningEffort


class Environment(StrEnum):
    LOCAL = "local"
    PRODUCTION = "production"


class PiiRedactorKind(StrEnum):
    DLP = "dlp"
    REGEX = "regex"


class ModelProfile(StrEnum):
    FREE_TIER = "free-tier"
    PRODUCTION = "production"


class CapabilityName(StrEnum):
    CHARTS = "charts"
    REPORT_LIBRARY = "report_library"
    EMAIL_REPORT = "email_report"


class MailerKind(StrEnum):
    OUTBOX = "outbox"
    SMTP = "smtp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OPSFLEET_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    env: Environment
    gcp_project: str = Field(min_length=6)
    bq_source_dataset: str = "bigquery-public-data.thelook_ecommerce"
    bq_max_bytes_billed: int = Field(default=2 * 1024**3, gt=0)
    bq_timeout_seconds: float = Field(default=60.0, gt=0)

    llm_base_url: str = "http://localhost:8080/v1"
    llm_api_key: SecretStr
    model_profile: ModelProfile = ModelProfile.FREE_TIER
    reasoning_effort: dict[AgentRole, ReasoningEffort] = Field(default_factory=dict)

    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    orchestrator_max_steps: int = Field(default=8, gt=0)
    analyst_max_steps: int = Field(default=6, gt=0)
    analyst_max_sql_failures: int = Field(default=3, gt=0)
    turn_token_budget: int = Field(default=200_000, gt=0)
    history_max_turns: int = Field(default=20, gt=0)
    history_max_chars: int = Field(default=20_000, gt=0)
    preview_rows: int = Field(default=20, gt=0)

    embedding_model: str = "gemini/gemini-embedding-001"
    golden_examples_limit: int = Field(default=6, ge=0, le=12)
    golden_min_similarity: float = Field(default=0.55, ge=0, le=1)
    golden_max_chars: int = Field(default=10_000, gt=0)
    golden_dataset: Path = Path("data/golden/trios.jsonl")
    summary_max_chars: int = Field(default=3_000, gt=0)
    analysis_state_max_steps: int = Field(default=5, gt=0)

    deletion_confirmation_ttl_seconds: int = Field(default=300, gt=0)
    artifacts_dir: Path = Path("var/artifacts")

    database_url: SecretStr
    pii_hash_salt: SecretStr = Field(min_length=16)
    pii_redactor: PiiRedactorKind = PiiRedactorKind.DLP
    dlp_location: str = "global"
    dlp_timeout_seconds: float = Field(default=10.0, gt=0)

    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    prompt_label: str = "production"
    prompt_cache_ttl_seconds: int = Field(default=60, ge=0)
    metrics_port: int | None = Field(default=None, gt=0, lt=65536)

    auth_issuer: str | None = None
    auth_jwks_url: str | None = None
    auth_audience: str | None = None
    auth_cli_client_id: str = "opsfleet-cli"
    redis_url: str | None = None
    rate_limit_per_minute: int = Field(default=10, gt=0)
    rate_limit_per_day: int = Field(default=200, gt=0)
    temporal_address: str | None = None
    temporal_namespace: str = "default"
    temporal_task_queue: str = "opsfleet"
    chart_bucket: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, gt=0, lt=65536)

    capabilities: Annotated[tuple[CapabilityName, ...], NoDecode] = tuple(CapabilityName)
    mailer: MailerKind = MailerKind.OUTBOX
    mail_outbox_dir: Path = Path("var/outbox")
    mail_from: str = Field(default="analyst@opsfleet.demo", pattern=r"^[^@\s]+@[^@\s]+$")
    user_email_template: str = Field(default="{user_id}@opsfleet.demo", pattern=r"\{user_id\}")
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, gt=0, lt=65536)
    smtp_starttls: bool = True
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_timeout_seconds: float = Field(default=10.0, gt=0)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _split_capabilities(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(name.strip() for name in value.split(",") if name.strip())
        return value

    @model_validator(mode="after")
    def _capabilities_unique(self) -> Self:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must not repeat")
        return self

    @model_validator(mode="after")
    def _smtp_requires_host(self) -> Self:
        if self.mailer is MailerKind.SMTP and not self.smtp_host:
            raise ValueError("mailer=smtp requires smtp_host")
        return self

    @model_validator(mode="after")
    def _user_email_template_renders(self) -> Self:
        self.user_email("probe")
        return self

    def user_email(self, user_id: str) -> str:
        try:
            address = self.user_email_template.format(user_id=user_id)
        except (KeyError, IndexError) as error:
            raise ValueError("user_email_template may only use {user_id}") from error
        if address.count("@") != 1:
            raise ValueError("user_email_template must render one email address")
        return address

    @model_validator(mode="after")
    def _langfuse_all_or_nothing(self) -> Self:
        parts = (self.langfuse_host, self.langfuse_public_key, self.langfuse_secret_key)
        if any(parts) and not all(parts):
            raise ValueError(
                "langfuse_host, langfuse_public_key and langfuse_secret_key go together"
            )
        return self

    @model_validator(mode="after")
    def _production_requires_auth(self) -> Self:
        if self.env is Environment.PRODUCTION and not (self.auth_issuer and self.auth_jwks_url):
            raise ValueError("production requires auth_issuer and auth_jwks_url")
        return self


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OPSFLEET_", extra="ignore")

    env: Environment
    api_url: str = "http://localhost:8000"
    trace_url: str | None = Field(default=None, pattern=r"\{trace_id\}")
    credentials_path: Path = Field(
        default_factory=lambda: Path.home() / ".config" / "opsfleet" / "credentials.json"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_client_settings() -> ClientSettings:
    return ClientSettings()
