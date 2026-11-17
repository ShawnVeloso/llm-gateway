"""Gateway settings, read from environment variables and `.env`."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every provider speaks the OpenAI chat-completions format, so adding one is a new entry here
# plus its settings below - no new request/streaming code.
Provider = Literal["gemini", "ollama", "openai", "anthropic"]
PROVIDERS: tuple[Provider, ...] = get_args(Provider)

# Not a setting on purpose: the gateway has no auth, so it must never listen beyond this PC.
HOST = "127.0.0.1"


@dataclass(frozen=True)
class ProviderEndpoint:
    base_url: str
    api_key: SecretStr | None
    needs_key: bool
    key_env_var: str | None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = Field(default=8787, ge=1, le=65535)

    gemini_api_key: SecretStr | None = None
    gemini_base_url: HttpUrl = HttpUrl("https://generativelanguage.googleapis.com/v1beta/openai/")
    openai_api_key: SecretStr | None = None
    openai_base_url: HttpUrl = HttpUrl("https://api.openai.com/v1/")
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: HttpUrl = HttpUrl("https://api.anthropic.com/v1/")
    ollama_base_url: HttpUrl = HttpUrl("http://localhost:11434/v1/")

    # Model-name prefix -> provider, matched case-insensitively. Longest matching prefix wins;
    # no match -> default_provider. "gpt-oss" is an open-weights model run locally via Ollama.
    model_prefixes: dict[str, Provider] = {
        "gemini-": "gemini",
        "gpt-": "openai",
        "gpt-oss": "ollama",
        "chatgpt-": "openai",
        "o1": "openai",
        "o3": "openai",
        "o4-": "openai",
        "claude-": "anthropic",
    }
    default_provider: Provider = "ollama"

    request_timeout_seconds: float = Field(default=120.0, gt=0)

    # Retries of transient failures (connect errors, timeouts, 429, 5xx), per provider.
    max_retries: int = Field(default=2, ge=0)
    retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    retry_max_delay_seconds: float = Field(default=8.0, ge=0)
    # Provider -> model to try once that provider's retries are used up. The model is routed like
    # a client's, so "ollama/qwen2.5" pins the provider. Fallbacks don't chain.
    fallback_models: dict[Provider, str] = {"gemini": "ollama/qwen2.5"}

    db_path: Path = Path("data/gateway.db")
    log_content: bool = False

    # Exact-match response cache, stored in the same DB. Clients skip it per request with a
    # `Cache-Control: no-cache` (fresh answer, still stored) or `no-store` header.
    cache_enabled: bool = True
    cache_ttl_seconds: float = Field(default=3600.0, gt=0)

    def endpoint(self, provider: Provider) -> ProviderEndpoint:
        if provider == "ollama":
            return ProviderEndpoint(str(self.ollama_base_url), None, False, None)
        base_url: HttpUrl = getattr(self, f"{provider}_base_url")
        api_key: SecretStr | None = getattr(self, f"{provider}_api_key")
        return ProviderEndpoint(str(base_url), api_key, True, f"{provider.upper()}_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
