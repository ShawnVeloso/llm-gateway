import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import Settings

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
}


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(
            Settings(_env_file=None, db_path=tmp_path / "gateway.db", retry_base_delay_seconds=0)
        )
    ) as client:
        yield client


def chat(model: str = "llama3.2", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@respx.mock
def test_chat_completion_returns_provider_response(client):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    response = client.post("/v1/chat/completions", json=chat())

    assert response.status_code == 200
    assert response.json() == COMPLETION


@respx.mock
def test_service_error_status_and_body_reach_client(client):
    response = client.post("/v1/chat/completions", json=chat("claude-sonnet-5"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "provider_not_configured"


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]"])
def test_body_must_be_a_json_object(client, content):
    response = client.post(
        "/v1/chat/completions", content=content, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


@respx.mock
def test_streaming_relays_sse_bytes(client):
    sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, content=sse, headers={"Content-Type": "text/event-stream"})
    )

    response = client.post("/v1/chat/completions", json=chat(stream=True))

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert response.content == sse


@respx.mock
def test_streaming_error_before_first_byte_is_a_json_error(client):
    respx.post(OLLAMA_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    response = client.post("/v1/chat/completions", json=chat(stream=True))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"
