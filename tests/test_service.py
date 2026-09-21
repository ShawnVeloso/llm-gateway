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


def make_settings(**overrides) -> Settings:
    # No backoff waits, so retry tests run instantly.
    return Settings(
        _env_file=None, gemini_api_key=GEMINI_KEY, retry_base_delay_seconds=0, **overrides
    )


@pytest.fixture
async def service():
    async with httpx.AsyncClient() as http:
        yield ChatService(make_settings(), http)


@pytest.fixture
async def no_fallback_service():
    async with httpx.AsyncClient() as http:
        yield ChatService(make_settings(fallback_models={}), http)


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
async def test_upstream_rate_limit_status_passes_through(no_fallback_service):
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(429, text="slow down"))

    result = await no_fallback_service.complete(chat("gemini-2.5-flash"))

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
    assert len(result.attempts) == 3  # retried; Ollama has no fallback


@respx.mock
async def test_non_json_success_body_is_a_bad_gateway(service):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, text="<html>proxy</html>"))

    result = await service.complete(chat("llama3.2"))

    assert result.status_code == 502
    assert result.body["error"]["code"] == "invalid_upstream_response"


# --- streaming ---

USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
SSE_EVENTS = [
    b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
    b'data: {"choices":[],"usage":' + json.dumps(USAGE).encode() + b"}\n\n",
    b"data: [DONE]\n\n",
]


def sse_response(chunks: list[bytes], error: Exception | None = None) -> httpx.Response:
    async def body():
        for chunk in chunks:
            yield chunk
        if error:
            raise error

    return httpx.Response(200, content=body(), headers={"Content-Type": "text/event-stream"})


async def collect(stream) -> bytes:
    return b"".join([chunk async for chunk in stream.chunks()])


@respx.mock
async def test_stream_relays_bytes_and_captures_usage(service):
    route = respx.post(GEMINI_URL).mock(return_value=sse_response(SSE_EVENTS))

    stream = await service.stream(
        chat("gemini-2.5-flash", stream=True, stream_options={"custom": 1})
    )
    relayed = await collect(stream)

    assert relayed == b"".join(SSE_EVENTS)
    assert stream.usage == USAGE
    assert stream.first_chunk_at is not None
    assert stream.error_message is None
    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is True
    # Client's own stream options are kept; include_usage is added.
    assert sent["stream_options"] == {"custom": 1, "include_usage": True}


@respx.mock
async def test_stream_usage_found_when_chunks_split_mid_line(service):
    whole = b"".join(SSE_EVENTS)
    pieces = [whole[i : i + 7] for i in range(0, len(whole), 7)]
    respx.post(GEMINI_URL).mock(return_value=sse_response(pieces))

    stream = await service.stream(chat("gemini-2.5-flash", stream=True))

    assert await collect(stream) == whole
    assert stream.usage == USAGE


@respx.mock
async def test_stream_without_usage_chunk_leaves_usage_none(service):
    respx.post(GEMINI_URL).mock(return_value=sse_response([SSE_EVENTS[0], SSE_EVENTS[3]]))

    stream = await service.stream(chat("gemini-2.5-flash", stream=True))
    await collect(stream)

    assert stream.usage is None


@respx.mock
async def test_stream_upstream_http_error_is_a_result_not_a_stream(service):
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": f"bad key {GEMINI_KEY}"}})
    )

    result = await service.stream(chat("gemini-2.5-flash", stream=True))

    assert result.status_code == 401
    assert "bad key" in result.body["error"]["message"]
    assert GEMINI_KEY not in result.body["error"]["message"]


async def test_stream_provider_not_configured(service):
    result = await service.stream(chat("claude-sonnet-5", stream=True))

    assert result.status_code == 400
    assert result.body["error"]["code"] == "provider_not_configured"


@respx.mock
async def test_stream_broken_mid_way_ends_with_error_event(service):
    respx.post(OLLAMA_URL).mock(
        return_value=sse_response(SSE_EVENTS[:1], error=httpx.ReadError("connection reset"))
    )

    stream = await service.stream(chat("llama3.2", stream=True))
    relayed = await collect(stream)

    assert relayed.startswith(SSE_EVENTS[0])
    last_event = json.loads(relayed.split(b"data: ")[-1])
    assert last_event["error"]["type"] == "upstream_error"
    assert "connection reset" in stream.error_message


# --- retries and fallback ---

FALLBACK_COMPLETION = {**COMPLETION, "model": "qwen2.5"}


def tries(outcome) -> list[tuple[str, int]]:
    return [(attempt.provider, attempt.status_code) for attempt in outcome.attempts]


@respx.mock
async def test_transient_error_is_retried_then_succeeds(service):
    gemini = respx.post(GEMINI_URL).mock(
        side_effect=[httpx.Response(503, text="overloaded"), httpx.Response(200, json=COMPLETION)]
    )

    result = await service.complete(chat("gemini-3.6-flash"))

    assert result.status_code == 200
    assert result.provider == "gemini"
    assert gemini.call_count == 2
    assert tries(result) == [("gemini", 503), ("gemini", 200)]
    assert "overloaded" in result.attempts[0].error_message
    assert result.attempts[1].error_message is None


@respx.mock
async def test_connect_error_is_retried(service):
    respx.post(OLLAMA_URL).mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=COMPLETION)]
    )

    result = await service.complete(chat("llama3.2"))

    assert result.status_code == 200
    assert tries(result) == [("ollama", 502), ("ollama", 200)]


@respx.mock
async def test_retries_exhausted_falls_back_to_configured_model(service):
    gemini = respx.post(GEMINI_URL).mock(return_value=httpx.Response(503))
    ollama = respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FALLBACK_COMPLETION))

    result = await service.complete(chat("gemini-3.6-flash", temperature=0.2))

    assert result.status_code == 200
    assert result.body == FALLBACK_COMPLETION
    assert (result.provider, result.model) == ("ollama", "qwen2.5")
    assert gemini.call_count == 3  # the first try + MAX_RETRIES=2
    assert tries(result) == [("gemini", 503)] * 3 + [("ollama", 200)]
    sent = ollama.calls.last.request
    assert "Authorization" not in sent.headers  # Gemini's key never goes to the fallback
    assert json.loads(sent.content) == chat("qwen2.5", temperature=0.2)


@pytest.mark.parametrize("status", [400, 401, 404])
@respx.mock
async def test_client_errors_are_not_retried_and_do_not_fall_back(service, status):
    gemini = respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(status, json={"error": {"message": "nope"}})
    )

    result = await service.complete(chat("gemini-3.6-flash"))

    # respx.mock would also fail on the unmocked Ollama call if it had fallen back.
    assert result.status_code == status
    assert gemini.call_count == 1
    assert tries(result) == [("gemini", status)]


@respx.mock
async def test_long_retry_after_skips_retries_and_falls_back(service):
    gemini = respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "60"})
    )
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FALLBACK_COMPLETION))

    result = await service.complete(chat("gemini-3.6-flash"))

    assert result.status_code == 200
    assert gemini.call_count == 1
    assert result.provider == "ollama"


@respx.mock
async def test_when_fallback_also_fails_its_error_is_returned(service):
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(503))
    respx.post(OLLAMA_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    result = await service.complete(chat("gemini-3.6-flash"))

    assert result.status_code == 502
    assert result.provider == "ollama"
    assert result.body["error"]["code"] == "upstream_unavailable"
    assert tries(result) == [("gemini", 503)] * 3 + [("ollama", 502)] * 3


@respx.mock
async def test_fallback_to_unconfigured_provider_is_skipped():
    settings = make_settings(fallback_models={"gemini": "anthropic/claude-sonnet-5"})
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        result = await ChatService(settings, http).complete(chat("gemini-3.6-flash"))

    assert result.status_code == 503
    assert tries(result) == [("gemini", 503)] * 3


@respx.mock
async def test_stream_falls_back_when_upstream_fails_before_streaming(service):
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(503))
    ollama = respx.post(OLLAMA_URL).mock(return_value=sse_response(SSE_EVENTS))

    stream = await service.stream(chat("gemini-3.6-flash", stream=True))

    assert await collect(stream) == b"".join(SSE_EVENTS)
    assert (stream.provider, stream.model) == ("ollama", "qwen2.5")
    assert tries(stream) == [("gemini", 503)] * 3 + [("ollama", 200)]
    sent = json.loads(ollama.calls.last.request.content)
    assert (sent["model"], sent["stream_options"]) == ("qwen2.5", {"include_usage": True})


@respx.mock
async def test_stream_broken_before_first_byte_is_retried(service):
    respx.post(GEMINI_URL).mock(
        side_effect=[
            sse_response([], error=httpx.ReadError("connection reset")),
            sse_response(SSE_EVENTS),
        ]
    )

    stream = await service.stream(chat("gemini-3.6-flash", stream=True))

    assert await collect(stream) == b"".join(SSE_EVENTS)
    assert tries(stream) == [("gemini", 502), ("gemini", 200)]
    assert "connection reset" in stream.attempts[0].error_message
    assert stream.error_message is None


@respx.mock
async def test_stream_broken_after_first_byte_is_not_retried(service):
    gemini = respx.post(GEMINI_URL).mock(
        return_value=sse_response(SSE_EVENTS[:1], error=httpx.ReadError("connection reset"))
    )

    stream = await service.stream(chat("gemini-3.6-flash", stream=True))
    relayed = await collect(stream)

    # The client already has the first bytes, so switching provider would corrupt its answer.
    assert relayed.startswith(SSE_EVENTS[0])
    assert "connection reset" in stream.error_message
    assert gemini.call_count == 1
    assert tries(stream) == [("gemini", 200)]
