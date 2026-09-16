"""Gateway settings, read from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["gemini", "ollama"]

# Not a setting on purpose: the gateway has no auth, so it must never listen beyond this PC.
HOST = "127.0.0.1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = Field(default=8787, ge=1, le=65535)

    gemini_api_key: SecretStr | None = None
    gemini_base_url: HttpUrl = HttpUrl("https://generativelanguage.googleapis.com/v1beta/openai/")
    ollama_base_url: HttpUrl = HttpUrl("http://localhost:11434/v1/")

    # Model-name prefix -> provider. Longest matching prefix wins; no match -> default_provider.
    model_prefixes: dict[str, Provider] = {"gemini-": "gemini"}
    default_provider: Provider = "ollama"

    request_timeout_seconds: float = Field(default=120.0, gt=0)

    db_path: Path = Path("data/gateway.db")
    log_content: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
