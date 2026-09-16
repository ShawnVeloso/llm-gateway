import json

import httpx
import pytest
import respx

from llm_gateway.config import Settings
from llm_gateway.service import ChatService

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
GEMINI_KEY = "g-secret-key-123"

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def service():
    settings = Settings(_env_file=None, gemini_api_key=GEMINI_KEY)
    async with httpx.AsyncClient() as http:
        yield ChatService(settings, http)


def chat(model: str, **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


@respx.mock
async def test_forwards_request_and_returns_provider_response(service):
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    result = await service.complete(chat("gemini/gemini-2.5-flash", temperature=0.2))

    assert result.status_code == 200
    assert result.body == COMPLETION
    assert (result.provider, result.model) == ("gemini", "gemini-2.5-flash")
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {GEMINI_KEY}"
    # Explicit "gemini/" prefix is stripped; other fields pass through untouched.
    assert json.loads(sent.content) == chat("gemini-2.5-flash", temperature=0.2)


@respx.mock
async def test_ollama_gets_no_auth_header_and_stream_flags_are_dropped(service):
    route = respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    result = await service.complete(
        chat("llama3.2", stream=True, stream_options={"include_usage": True})
    )

    assert result.status_code == 200
    sent = route.calls.last.request
    assert "Authorization" not in sent.headers
    assert json.loads(sent.content) == chat("llama3.2")


async def test_base_url_without_trailing_slash_keeps_path():
    settings = Settings(_env_file=None, ollama_base_url="http://localhost:11434/v1")
    async with respx.mock() as mock, httpx.AsyncClient() as http:
        route = mock.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=COMPLETION))
        await ChatService(settings, http).complete(chat("llama3.2"))
    assert route.called


@respx.mock
async def test_provider_not_configured_returns_error_without_calling_upstream(service):
    result = await service.complete(chat("claude-sonnet-5"))

    assert result.status_code == 400
    error = result.body["error"]
    assert error["code"] == "provider_not_configured"
    assert error["type"] == "invalid_request_error"
    assert "ANTHROPIC_API_KEY" in error["message"]
    assert not respx.calls  # respx.mock would also fail on any unmatched request


@pytest.mark.parametrize("body", [{"messages": []}, {"model": 5, "messages": []}])
async def test_missing_or_invalid_model_is_rejected(service, body):
    result = await service.complete(body)

    assert result.status_code == 400
    assert result.body["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize(
    "upstream_body",
    [
        {"error": {"message": f"API key {GEMINI_KEY} not valid", "code": 400}},
        # Gemini sometimes wraps the error object in a list.
        [{"error": {"message": f"API key {GEMINI_KEY} not valid", "code": 400}}],
    ],
)
@respx.mock
async def test_upstream_error_keeps_status_and_redacts_key(service, upstream_body):
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(400, json=upstream_body))

    result = await service.complete(chat("gemini-2.5-flash"))

    assert result.status_code == 400
    error = result.body["error"]
    assert error["type"] == "upstream_error"
    assert "not valid" in error["message"]
    assert GEMINI_KEY not in json.dumps(result.body)
    assert GEMINI_KEY not in (result.error_message or "")


@respx.mock
async def test_upstream_rate_limit_status_passes_through(service):
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(429, text="slow down"))

    result = await service.complete(chat("gemini-2.5-flash"))

    assert result.status_code == 429
    assert "slow down" in result.body["error"]["message"]


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (httpx.ConnectError("connection refused"), 502, "upstream_unavailable"),
        (httpx.ReadTimeout("timed out"), 504, "upstream_timeout"),
    ],
)
@respx.mock
async def test_transport_failures_become_gateway_errors(service, exc, status, code):
    respx.post(OLLAMA_URL).mock(side_effect=exc)

    result = await service.complete(chat("llama3.2"))

    assert result.status_code == status
    assert result.body["error"]["code"] == code
    assert result.provider == "ollama"


@respx.mock
async def test_non_json_success_body_is_a_bad_gateway(service):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, text="<html>proxy</html>"))

    result = await service.complete(chat("llama3.2"))

    assert result.status_code == 502
    assert result.body["error"]["code"] == "invalid_upstream_response"
