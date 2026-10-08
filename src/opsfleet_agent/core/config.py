from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsfleet_agent.core.roles import AgentRole, ReasoningEffort


class Environment(StrEnum):
    LOCAL = "local"
    PRODUCTION = "production"


class ModelProfile(StrEnum):
    FREE_TIER = "free-tier"
    PRODUCTION = "production"


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

    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    prompt_label: str = "production"
    prompt_cache_ttl_seconds: int = Field(default=60, ge=0)
    metrics_port: int | None = Field(default=None, gt=0, lt=65536)

    auth_issuer: str | None = None
    auth_jwks_url: str | None = None
    auth_audience: str | None = None
    redis_url: str | None = None
    rate_limit_per_minute: int = Field(default=10, gt=0)
    rate_limit_per_day: int = Field(default=200, gt=0)
    temporal_address: str | None = None
    temporal_namespace: str = "default"
    temporal_task_queue: str = "opsfleet"
    chart_bucket: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, gt=0, lt=65536)

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_client_settings() -> ClientSettings:
    return ClientSettings()
