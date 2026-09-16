import pytest

from llm_gateway.config import Settings
from llm_gateway.routing import ProviderNotConfiguredError, Route, resolve_route


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        gemini_api_key="g-key",
        openai_api_key="o-key",
        anthropic_api_key="a-key",
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gemini-2.5-flash", Route("gemini", "gemini-2.5-flash")),
        ("gpt-5", Route("openai", "gpt-5")),
        ("o3-mini", Route("openai", "o3-mini")),
        ("claude-sonnet-5", Route("anthropic", "claude-sonnet-5")),
        ("Claude-Sonnet-5", Route("anthropic", "Claude-Sonnet-5")),
        ("llama3.2", Route("ollama", "llama3.2")),
        # Longest prefix wins: "gpt-oss" beats "gpt-".
        ("gpt-oss:20b", Route("ollama", "gpt-oss:20b")),
        # Explicit provider prefix overrides the prefix map and is stripped before sending.
        ("ollama/gpt-5-local", Route("ollama", "gpt-5-local")),
        ("anthropic/claude-opus-5", Route("anthropic", "claude-opus-5")),
        # A slash that isn't a provider name is part of an Ollama model name.
        ("hf.co/user/model", Route("ollama", "hf.co/user/model")),
    ],
)
def test_routes_model_to_provider(settings, model, expected):
    assert resolve_route(model, settings) == expected


def test_default_provider_is_configurable(clean_env):
    s = Settings(_env_file=None, default_provider="gemini", gemini_api_key="g-key")
    assert resolve_route("some-new-model", s).provider == "gemini"


def test_custom_prefix_from_env(clean_env, monkeypatch):
    monkeypatch.setenv("MODEL_PREFIXES", '{"mistral": "ollama", "grok-": "openai"}')
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    s = Settings(_env_file=None)
    assert resolve_route("grok-4", s).provider == "openai"


@pytest.mark.parametrize(
    ("model", "env_var"),
    [("claude-sonnet-5", "ANTHROPIC_API_KEY"), ("gpt-5", "OPENAI_API_KEY")],
)
def test_missing_key_gives_clear_error(clean_env, model, env_var):
    s = Settings(_env_file=None)
    with pytest.raises(ProviderNotConfiguredError, match=env_var):
        resolve_route(model, s)


def test_ollama_needs_no_key(clean_env):
    assert resolve_route("qwen3", Settings(_env_file=None)).provider == "ollama"
