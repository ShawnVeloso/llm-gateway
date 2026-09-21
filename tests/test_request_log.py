import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.request_log import CLIENT_DISCONNECTED, MIGRATIONS, RequestLog, RequestRecord

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
GEMINI_KEY = "g-secret-key-123"
USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi there"}}],
    "usage": USAGE,
}
SSE = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n'
    b'data: {"choices":[],"usage":' + json.dumps(USAGE).encode() + b"}\n\n"
    b"data: [DONE]\n\n"
)


def make_client(tmp_path, **settings) -> tuple[TestClient, RequestLog]:
    db_path = tmp_path / "logs" / "gateway.db"  # missing folder: startup must create it
    app = create_app(
        Settings(
            _env_file=None,
            db_path=db_path,
            gemini_api_key=GEMINI_KEY,
            retry_base_delay_seconds=0,
            **settings,
        )
    )
    return TestClient(app), RequestLog(db_path)


def chat(model: str = "llama3.2", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


def sse_response(content) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})


@respx.mock
def test_normal_request_writes_one_row(tmp_path):
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    client, log = make_client(tmp_path)

    with client:
        client.post(
            "/v1/chat/completions", json=chat("gemini-2.5-flash"), headers={"X-App-Name": "lithe"}
        )
        [row] = log.rows()

    assert row["app"] == "lithe"
    assert row["provider"] == "gemini"
    assert row["model"] == "gemini-2.5-flash"
    assert row["is_stream"] == 0
    assert (row["input_tokens"], row["output_tokens"]) == (3, 2)
    assert row["status_code"] == 200
    assert row["latency_ms"] >= 0
    assert row["ttft_ms"] is None
    assert row["error_message"] is None
    assert len(row["request_id"]) == 32
    assert row["created_at"].endswith("+00:00")


@respx.mock
def test_missing_app_header_and_usage_log_unknown_and_null_tokens(tmp_path):
    no_usage = {k: v for k, v in COMPLETION.items() if k != "usage"}
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=no_usage))
    client, log = make_client(tmp_path)

    with client:
        client.post("/v1/chat/completions", json=chat())
        [row] = log.rows()

    assert row["app"] == "unknown"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None


@respx.mock
def test_streaming_request_logs_usage_and_ttft_after_stream_ends(tmp_path):
    respx.post(OLLAMA_URL).mock(return_value=sse_response(SSE))
    client, log = make_client(tmp_path)

    with client:
        response = client.post("/v1/chat/completions", json=chat(stream=True))
        assert response.content == SSE
        [row] = log.rows()

    assert row["is_stream"] == 1
    assert row["provider"] == "ollama"
    assert (row["input_tokens"], row["output_tokens"]) == (3, 2)
    assert row["status_code"] == 200
    assert 0 <= row["ttft_ms"] <= row["latency_ms"]
    assert row["error_message"] is None


@respx.mock
def test_stream_broken_mid_way_logs_error(tmp_path):
    async def body():
        yield SSE.split(b"\n\n")[0] + b"\n\n"
        raise httpx.ReadError("connection reset")

    respx.post(OLLAMA_URL).mock(return_value=sse_response(body()))
    client, log = make_client(tmp_path)

    with client:
        client.post("/v1/chat/completions", json=chat(stream=True))
        [row] = log.rows()

    assert row["status_code"] == 200
    assert "connection reset" in row["error_message"]
    assert row["input_tokens"] is None


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
def test_provider_error_is_logged_without_api_key(tmp_path, stream):
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": f"bad key {GEMINI_KEY}"}})
    )
    client, log = make_client(tmp_path, log_content=True)

    with client:
        client.post("/v1/chat/completions", json=chat("gemini-2.5-flash", stream=stream))
        [row] = log.rows()

    assert row["status_code"] == 401
    assert row["is_stream"] == int(stream)
    assert "bad key" in row["error_message"]
    dump = "\n".join(sqlite3.connect(log.path).iterdump())
    assert GEMINI_KEY not in dump


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
def test_fallback_logs_every_attempt_and_the_serving_provider(tmp_path, stream):
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(503, json={"error": {"message": f"busy {GEMINI_KEY}"}})
    )
    respx.post(OLLAMA_URL).mock(
        return_value=sse_response(SSE) if stream else httpx.Response(200, json=COMPLETION)
    )
    client, log = make_client(tmp_path)

    with client:
        response = client.post("/v1/chat/completions", json=chat("gemini-3.6-flash", stream=stream))
        [row] = log.rows()
        attempts = log.rows("attempts")

    assert response.status_code == 200
    assert (row["provider"], row["model"]) == ("ollama", "qwen2.5")
    assert row["requested_model"] == "gemini-3.6-flash"
    assert row["status_code"] == 200
    assert row["attempt_count"] == 4
    assert [(a["attempt_number"], a["provider"], a["status_code"]) for a in attempts] == [
        (1, "gemini", 503),
        (2, "gemini", 503),
        (3, "gemini", 503),
        (4, "ollama", 200),
    ]
    assert {a["request_id"] for a in attempts} == {row["request_id"]}
    assert "busy" in attempts[0]["error_message"]
    assert attempts[3]["model"] == "qwen2.5"
    dump = "\n".join(sqlite3.connect(log.path).iterdump())
    assert GEMINI_KEY not in dump


def test_stage_1_log_is_upgraded_in_place(tmp_path):
    log = RequestLog(tmp_path / "gateway.db")
    with sqlite3.connect(log.path) as db:  # what Stage 1 left behind: no user_version
        db.execute(MIGRATIONS[0])
        db.execute(
            "INSERT INTO requests (request_id, created_at, app, is_stream, latency_ms, "
            "status_code) VALUES ('old', '2026-09-01T00:00:00+00:00', 'lithe', 0, 1.0, 200)"
        )

    log.create()
    log.create()  # a second startup changes nothing

    [row] = log.rows()
    assert row["request_id"] == "old"
    assert row["attempt_count"] is None  # unknown for rows from before Stage 2
    assert log.rows("attempts") == []
    with closing(sqlite3.connect(log.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (len(MIGRATIONS),)


def test_provider_not_configured_is_logged(tmp_path):
    client, log = make_client(tmp_path)

    with client:
        client.post("/v1/chat/completions", json=chat("claude-sonnet-5"))
        [row] = log.rows()

    assert row["status_code"] == 400
    assert row["provider"] is None
    assert row["model"] == "claude-sonnet-5"
    assert row["attempt_count"] == 0
    assert "ANTHROPIC_API_KEY" in row["error_message"]


@respx.mock
def test_content_not_stored_by_default(tmp_path):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    client, log = make_client(tmp_path)

    with client:
        client.post("/v1/chat/completions", json=chat())
        [row] = log.rows()

    assert row["request_content"] is None
    assert row["response_content"] is None


@respx.mock
@pytest.mark.parametrize("stream", [False, True])
def test_content_stored_when_enabled(tmp_path, stream):
    response = sse_response(SSE) if stream else httpx.Response(200, json=COMPLETION)
    respx.post(OLLAMA_URL).mock(return_value=response)
    client, log = make_client(tmp_path, log_content=True)

    with client:
        client.post("/v1/chat/completions", json=chat(stream=stream))
        [row] = log.rows()

    assert json.loads(row["request_content"]) == [{"role": "user", "content": "hello"}]
    assert row["response_content"] == "hi there"


@respx.mock
def test_log_write_failure_does_not_fail_the_request(tmp_path):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    client, log = make_client(tmp_path)

    with client:
        with sqlite3.connect(log.path) as db:
            db.execute("DROP TABLE requests")
        response = client.post("/v1/chat/completions", json=chat())

    assert response.status_code == 200


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@respx.mock
async def test_client_disconnect_mid_stream_still_logs(tmp_path):
    respx.post(OLLAMA_URL).mock(return_value=sse_response(SSE))
    db_path = tmp_path / "gateway.db"
    app = create_app(Settings(_env_file=None, db_path=db_path))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8787),
    }
    request_body = json.dumps(chat(stream=True)).encode()

    async def receive():
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message):
        # What uvicorn does when the client has hung up: sending body bytes fails.
        if message["type"] == "http.response.body" and message["body"]:
            raise OSError("client went away")

    async with app.router.lifespan_context(app):
        with pytest.raises(Exception):  # noqa: B017 - Starlette's ClientDisconnect, wrapped or not
            await app(scope, receive, send)

    [row] = RequestLog(db_path).rows()
    assert row["is_stream"] == 1
    assert row["error_message"] == CLIENT_DISCONNECTED


@pytest.mark.anyio
async def test_write_completes_even_if_caller_is_cancelled(tmp_path):
    # On a client disconnect the server cancels the request task while the row is being written.
    log = RequestLog(tmp_path / "gateway.db")
    log.create()
    empty = dict.fromkeys(RequestRecord.__dataclass_fields__)
    fields = {
        "app": "test",
        "is_stream": False,
        "latency_ms": 1.0,
        "status_code": 200,
        "attempts": (),
    }
    record = RequestRecord(**{**empty, **fields, "created_at": "2026-01-01T00:00:00+00:00"})

    for _ in range(20):  # the unprotected version loses the race most of the time, not always
        task = asyncio.create_task(log.write(replace(record, request_id=uuid4().hex)))
        await asyncio.sleep(0)  # let it hand the insert to a worker thread
        task.cancel()
    await log.wait_for_pending_writes()

    assert len(log.rows()) == 20
