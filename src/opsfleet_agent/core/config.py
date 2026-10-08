from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    env: Environment = Environment.LOCAL
    gcp_project: str = Field(min_length=6)
    bq_source_dataset: str = "bigquery-public-data.thelook_ecommerce"
    bq_max_bytes_billed: int = Field(default=2 * 1024**3, gt=0)
    bq_timeout_seconds: float = Field(default=60.0, gt=0)

    llm_base_url: str = "http://localhost:8080/v1"
    llm_api_key: SecretStr
    model_profile: ModelProfile = ModelProfile.FREE_TIER

    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    orchestrator_max_steps: int = Field(default=8, gt=0)
    analyst_max_steps: int = Field(default=10, gt=0)
    analyst_max_sql_failures: int = Field(default=3, gt=0)
    history_max_turns: int = Field(default=20, gt=0)
    history_max_chars: int = Field(default=20_000, gt=0)
    preview_rows: int = Field(default=20, gt=0)

    database_url: SecretStr
    pii_hash_salt: SecretStr = Field(min_length=16)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
