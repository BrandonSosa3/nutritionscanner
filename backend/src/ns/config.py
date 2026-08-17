"""Application configuration.

Every value comes from the environment. Nothing has a production-safe default
that could silently mask a missing variable — if a required setting is absent,
the process fails at import with a message naming the variable.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",  # an unrecognised env var is a typo, not a feature
    )

    environment: Environment = Environment.LOCAL

    database_url: PostgresDsn
    redis_url: RedisDsn

    # Optional so the app boots for migrations, health checks, and CI without
    # a key. The extraction stage raises a clear error if it's missing.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"

    usda_api_key: SecretStr | None = None

    # Where receipt images live. Personal financial records — see .gitignore.
    receipt_storage_path: Path = Path("data/receipts")

    # Reconciliation tolerance in cents. A receipt whose line items differ from
    # the printed total by more than this is flagged suspect, never persisted
    # as clean.
    reconciliation_tolerance_cents: int = 2

    # Deliberately a plain string, not list[str]. pydantic-settings JSON-decodes
    # complex-typed fields straight from the environment before any validator
    # runs, so a `list[str]` here rejects the comma-separated form that every
    # PaaS dashboard produces. Parsed by `cors_origin_list` below.
    cors_origins: str = "http://localhost:5173"

    log_level: str = "INFO"
    log_json: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def sync_database_url(self) -> str:
        """Alembic's autogenerate path uses a sync driver."""
        return str(self.database_url).replace("postgresql+asyncpg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is read once. Raises at first call if a
    required variable is missing, naming the variable."""
    return Settings()
