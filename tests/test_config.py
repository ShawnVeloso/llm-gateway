from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_gateway.config import Settings


def make_settings(**overrides) -> Settings:
    # _env_file=None keeps a developer's real .env out of tests.
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "PORT",
        "GEMINI_API_KEY",
        "GEMINI_BASE_URL",
        "OLLAMA_BASE_URL",
        "MODEL_PREFIXES",
        "DEFAULT_PROVIDER",
        "REQUEST_TIMEOUT_SECONDS",
        "DB_PATH",
        "LOG_CONTENT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults():
    s = make_settings()
    assert s.port == 8787
    assert s.gemini_api_key is None
    assert s.default_provider == "ollama"
    assert s.model_prefixes == {"gemini-": "gemini"}
    assert s.db_path == Path("data/gateway.db")
    assert s.log_content is False


def test_reads_env_vars(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("MODEL_PREFIXES", '{"gemini-": "gemini", "llama": "ollama"}')
    monkeypatch.setenv("LOG_CONTENT", "true")
    s = make_settings()
    assert s.gemini_api_key.get_secret_value() == "sk-test-secret"
    assert s.model_prefixes == {"gemini-": "gemini", "llama": "ollama"}
    assert s.log_content is True


def test_api_key_hidden_in_repr():
    s = make_settings(gemini_api_key="sk-test-secret")
    assert "sk-test-secret" not in repr(s)
    assert "sk-test-secret" not in str(s.model_dump())


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_provider": "openai"},
        {"model_prefixes": {"gpt-": "openai"}},
        {"port": 0},
        {"request_timeout_seconds": 0},
    ],
)
def test_rejects_invalid_values(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)
