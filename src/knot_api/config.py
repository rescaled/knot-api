"""Application settings, loaded from the environment or an .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KNOT_API_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    token: SecretStr
    zones_dir: Path = Path("/var/lib/knot/zones")
    knot_socket: Path = Path("/run/knot/knot.sock")
    knot_timeout: int = Field(default=10, gt=0, description="Control socket timeout (s)")
    reload_timeout: int = Field(default=60, gt=0, description="Blocking zone-reload timeout (s)")
    txn_retries: int = Field(default=5, ge=1)
    txn_retry_base_delay: float = Field(default=0.25, gt=0)
    zone_template: str = "member"
    catalog_zone: str | None = None
    protected_zones: Annotated[list[str], NoDecode] = []
    kzonecheck_bin: str = "kzonecheck"
    kzonecheck_timeout: int = Field(default=10, gt=0)
    max_zonefile_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    libknot_so: str | None = None
    abort_stale_txn_on_startup: bool = False
    log_level: str = "INFO"

    @field_validator("protected_zones", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # token comes from the environment
