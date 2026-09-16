import pytest

from llm_gateway.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Drop real settings env vars (e.g. a machine-wide OPENAI_API_KEY) so tests are repeatable."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
