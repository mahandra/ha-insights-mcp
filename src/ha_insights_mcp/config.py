"""Runtime settings loaded from environment variables or a `.env` file."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ha_url: str = Field(default="http://localhost:8123", description="Home Assistant base URL")
    # SecretStr keeps the token out of repr(), str() and accidental logging.
    ha_token: SecretStr = Field(description="Home Assistant long-lived access token")
    ha_timeout_seconds: float = Field(default=10.0, gt=0)
    ha_cache_ttl_seconds: float = Field(default=10.0, ge=0)
    # Device health thresholds
    battery_low_percent: float = Field(default=20, ge=0, le=100)
    stale_hours: float = Field(default=24, gt=0)
    flapping_changes_per_hour: int = Field(default=20, ge=2)
    # Guarded actions (off by default: the server is read-only unless enabled)
    actions_enabled: bool = False
    actions_policy_path: Path = Path("policy.toml")
    audit_db_path: Path = Path("data/audit.db")
    log_level: str = "INFO"

    @field_validator("ha_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("HA_URL must start with http:// or https://")
        return value

    @field_validator("ha_token")
    @classmethod
    def _non_empty_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("HA_TOKEN is empty")
        return value
