"""Service configuration.

Follows 12-Factor App (III. Config): every setting comes from environment
variables. No .env file is read at runtime; for local development load it
explicitly, e.g. ``uv run --env-file .env python -m extractor``.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables, validated at startup."""

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8001, ge=1, le=65535)
    MAX_UPLOAD_SIZE: int = Field(default=50 * 1024 * 1024, gt=0)  # bytes
    THREAD_POOL_SIZE: int = Field(default=4, gt=0)
    MAX_QUEUE_WAIT: float = Field(default=20.0, gt=0)
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
