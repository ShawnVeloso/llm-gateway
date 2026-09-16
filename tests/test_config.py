from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_gateway.config import Settings


def make_settings(**overrides) -> Settings:
    # _env_file=None keeps a developer's real .env out of tests.
    return Settings(_env_file=None, **overrides)


def test_defaults():
    s = make_settings()
    assert s.port == 8787
    assert s.gemini_api_key is None
    assert s.default_provider == "ollama"
    assert s.model_prefixes["claude-"] == "anthropic"
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


def test_endpoint_per_provider():
    s = make_settings(anthropic_api_key="a-key")
    anthropic = s.endpoint("anthropic")
    assert anthropic.base_url == "https://api.anthropic.com/v1/"
    assert anthropic.api_key.get_secret_value() == "a-key"
    assert anthropic.key_env_var == "ANTHROPIC_API_KEY"
    assert s.endpoint("openai").api_key is None
    assert s.endpoint("ollama").needs_key is False


def test_api_key_hidden_in_repr():
    s = make_settings(gemini_api_key="sk-test-secret")
    assert "sk-test-secret" not in repr(s)
    assert "sk-test-secret" not in str(s.model_dump())


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_provider": "mistral"},
        {"model_prefixes": {"mistral-": "mistral"}},
        {"port": 0},
        {"request_timeout_seconds": 0},
    ],
)
def test_rejects_invalid_values(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)
