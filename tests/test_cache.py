import json
import sqlite3

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.cache import CacheControl, CacheEntry, ResponseCache, cache_key
from llm_gateway.config import Settings
from llm_gateway.request_log import RequestLog

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def completion(text: str = "hi there") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": USAGE,
    }


SSE = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n'
    b'data: {"choices":[],"usage":' + json.dumps(USAGE).encode() + b"}\n\n"
    b"data: [DONE]\n\n"
)


def make_client(tmp_path, **settings) -> tuple[TestClient, RequestLog]:
    db_path = tmp_path / "gateway.db"
    app = create_app(
        Settings(
            _env_file=None,
            db_path=db_path,
            gemini_api_key="g-key",
            retry_base_delay_seconds=0,
            **settings,
        )
    )
    return TestClient(app), RequestLog(db_path)


def chat(model: str = "llama3.2", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


def sse_response(content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})


def post(client: TestClient, body: dict, **headers: str) -> httpx.Response:
    return client.post("/v1/chat/completions", json=body, headers=headers)


def test_key_ignores_key_order_and_stream_options():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    b = {"temperature": 0, "messages": [{"content": "hi", "role": "user"}], "model": "m"}

    assert cache_key(a, is_stream=False) == cache_key(b, is_stream=False)
    assert cache_key(a, is_stream=True) == cache_key(
        {**b, "stream": True, "stream_options": {"include_usage": True}}, is_stream=True
    )


@pytest.mark.parametrize(
    "change", [{"model": "other"}, {"temperature": 0.7}, {"messages": []}, {"user": "x"}]
)
def test_key_changes_with_any_parameter(change):
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}

    assert cache_key(body, is_stream=False) != cache_key({**body, **change}, is_stream=False)


def test_key_separates_streams_from_normal_requests():
    body = chat()

    assert cache_key(body, is_stream=False) != cache_key(body, is_stream=True)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, CacheControl(read=True, write=True)),
        ("max-age=60", CacheControl(read=True, write=True)),
        ("no-cache", CacheControl(read=False, write=True)),
        ("No-Cache, max-age=0", CacheControl(read=False, write=True)),
        ("no-store", CacheControl(read=False, write=False)),
        ("no-cache, no-store", CacheControl(read=False, write=False)),
    ],
)
def test_cache_control_header(header, expected):
    assert CacheControl.from_header(header) == expected


@respx.mock
def test_identical_request_is_served_from_cache(tmp_path):
    upstream = respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=completion()))
    client, log = make_client(tmp_path)

    with client:
        first = post(client, chat())
        second = post(client, chat())
        miss, hit = log.rows()

    assert upstream.call_count == 1
    assert second.status_code == 200
    assert second.json() == first.json() == completion()
    assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("MISS", "HIT")
    assert (miss["cache_status"], hit["cache_status"]) == ("miss", "hit")
    assert (hit["provider"], hit["model"]) == ("ollama", "llama3.2")
    assert hit["attempt_count"] == 0  # no provider was called
    assert (hit["input_tokens"], hit["output_tokens"]) == (3, 2)  # as the cached response says
    assert len(log.rows("attempts")) == 1


@respx.mock
def test_identical_stream_is_replayed_from_cache(tmp_path):
    upstream = respx.post(OLLAMA_URL).mock(return_value=sse_response(SSE))
    client, log = make_client(tmp_path, log_content=True)

    with client:
        first = post(client, chat(stream=True))
        second = post(client, chat(stream=True))
        _, hit = log.rows()

    assert upstream.call_count == 1
    assert second.content == first.content == SSE
    assert second.headers["Content-Type"].startswith("text/event-stream")
    assert second.headers["X-Cache"] == "HIT"
    assert hit["cache_status"] == "hit"
    assert hit["is_stream"] == 1
    assert hit["attempt_count"] == 0
    assert (hit["input_tokens"], hit["output_tokens"]) == (3, 2)
    assert hit["ttft_ms"] is not None
    assert hit["response_content"] == "hi there"


@respx.mock
def test_stream_and_normal_request_are_cached_separately(tmp_path):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=completion()))
    client, _ = make_client(tmp_path)

    with client:
        post(client, chat())
        respx.post(OLLAMA_URL).mock(return_value=sse_response(SSE))
        response = post(client, chat(stream=True))

    assert response.headers["X-Cache"] == "MISS"
    assert response.content == SSE


@respx.mock
def test_no_cache_header_gets_a_fresh_answer_and_stores_it(tmp_path):
    upstream = respx.post(OLLAMA_URL)
    upstream.side_effect = [
        httpx.Response(200, json=completion("old")),
        httpx.Response(200, json=completion("new")),
    ]
    client, log = make_client(tmp_path)

    with client:
        post(client, chat())
        fresh = post(client, chat(), **{"Cache-Control": "no-cache"})
        after = post(client, chat())
        statuses = [row["cache_status"] for row in log.rows()]

    assert upstream.call_count == 2
    assert fresh.headers["X-Cache"] == "BYPASS"
    assert after.json()["choices"][0]["message"]["content"] == "new"
    assert statuses == ["miss", "bypass", "hit"]


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
def test_no_store_header_does_not_store(tmp_path, stream):
    response = sse_response(SSE) if stream else httpx.Response(200, json=completion())
    upstream = respx.post(OLLAMA_URL).mock(return_value=response)
    client, _ = make_client(tmp_path)

    with client:
        post(client, chat(stream=stream), **{"Cache-Control": "no-store"})
        second = post(client, chat(stream=stream))

    assert upstream.call_count == 2
    assert second.headers["X-Cache"] == "MISS"


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
def test_errors_are_not_cached(tmp_path, stream):
    upstream = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )
    client, _ = make_client(tmp_path)

    with client:
        post(client, chat(stream=stream))
        second = post(client, chat(stream=stream))

    assert upstream.call_count == 2
    assert second.status_code == 400
    assert second.headers["X-Cache"] == "MISS"


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
def test_fallback_answers_are_not_cached(tmp_path, stream):
    # Otherwise the fallback's answer would keep being served after Gemini is back.
    gemini = respx.post(GEMINI_URL).mock(return_value=httpx.Response(503))
    response = sse_response(SSE) if stream else httpx.Response(200, json=completion())
    respx.post(OLLAMA_URL).mock(return_value=response)
    client, _ = make_client(tmp_path, max_retries=0)

    with client:
        first = post(client, chat("gemini-3.6-flash", stream=stream))
        second = post(client, chat("gemini-3.6-flash", stream=stream))

    assert first.status_code == 200
    assert gemini.call_count == 2
    assert second.headers["X-Cache"] == "MISS"


@respx.mock
def test_stream_broken_mid_way_is_not_cached(tmp_path):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise httpx.ReadError("connection reset")

    upstream = respx.post(OLLAMA_URL)
    upstream.side_effect = [httpx.Response(200, stream=Broken()), sse_response(SSE)]
    client, _ = make_client(tmp_path)

    with client:
        post(client, chat(stream=True))
        second = post(client, chat(stream=True))

    assert upstream.call_count == 2
    assert second.content == SSE


@respx.mock
def test_cache_disabled_always_calls_provider(tmp_path):
    upstream = respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=completion()))
    client, log = make_client(tmp_path, cache_enabled=False)

    with client:
        post(client, chat())
        second = post(client, chat())
        rows = log.rows()

    assert upstream.call_count == 2
    assert "X-Cache" not in second.headers
    assert [row["cache_status"] for row in rows] == [None, None]


@respx.mock
def test_cache_failure_does_not_fail_the_request(tmp_path):
    upstream = respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=completion()))
    client, log = make_client(tmp_path)

    with client:
        with sqlite3.connect(log.path) as db:
            db.execute("DROP TABLE cache")
        first = post(client, chat())
        second = post(client, chat())

    assert (first.status_code, second.status_code) == (200, 200)
    assert upstream.call_count == 2


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_entries_expire_after_the_ttl(tmp_path, monkeypatch):
    log = RequestLog(tmp_path / "gateway.db")
    log.create()
    cache = ResponseCache(log.path, ttl_seconds=60)
    now = 1_000_000.0
    monkeypatch.setattr("llm_gateway.cache.time.time", lambda: now)
    await cache.put("k", CacheEntry("ollama", "llama3.2", b"{}"))

    now += 59
    assert await cache.get("k") == CacheEntry("ollama", "llama3.2", b"{}")
    now += 1
    assert await cache.get("k") is None

    await cache.put("other", CacheEntry("ollama", "llama3.2", b"{}"))  # writes clear old rows
    with sqlite3.connect(log.path) as db:
        assert db.execute("SELECT key FROM cache").fetchall() == [("other",)]
