"""Request logic behind `/v1/chat/completions`: route, forward, and turn every failure into an
OpenAI-style error. Later stages (retry, fallback, cache) and logging (`request_log.LoggedChat`)
wrap `ChatService.complete` and `ChatService.stream`."""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from llm_gateway.config import Provider, ProviderEndpoint, Settings
from llm_gateway.providers import open_chat_completions_stream, post_chat_completions
from llm_gateway.routing import ProviderNotConfiguredError, Route, resolve_route

MAX_UPSTREAM_ERROR_CHARS = 500


@dataclass(frozen=True)
class ChatResult:
    status_code: int
    body: dict[str, Any]
    provider: Provider | None = None  # None when the request failed before routing
    model: str | None = None  # name sent upstream
    error_message: str | None = None  # already redacted; safe to log


class ChatStream:
    """An open upstream SSE stream. Iterate `chunks()` to relay it; read the stats afterwards."""

    def __init__(self, response: httpx.Response, route: Route, secrets: list[str]) -> None:
        self.provider: Provider = route.provider
        self.model = route.model
        self.usage: dict[str, Any] | None = None  # from the provider's usage chunk, if it sent one
        self.first_chunk_at: float | None = None  # time.perf_counter() of the first body bytes
        self.error_message: str | None = None  # set if the stream broke part-way; redacted
        self.content_parts: list[str] = []  # assistant text deltas, in order
        self._response = response
        self._secrets = secrets
        self._buffer = b""

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield the provider's bytes unchanged, as they arrive."""
        try:
            async for chunk in self._response.aiter_bytes():
                if self.first_chunk_at is None:
                    self.first_chunk_at = time.perf_counter()
                self._scan(chunk)
                yield chunk
            self._scan_line(self._buffer)
        except httpx.HTTPError as exc:
            # Headers (200) are already sent, so the status can't change. Tell the client in-band,
            # the way OpenAI reports mid-stream errors.
            self.error_message = _redact(
                f"Stream from provider '{self.provider}' failed: {type(exc).__name__}: {exc}",
                self._secrets,
            )
            error = {"message": self.error_message, "type": "upstream_error", "code": None}
            yield f"data: {json.dumps({'error': error})}\n\n".encode()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await self._response.aclose()

    def _scan(self, chunk: bytes) -> None:
        # Chunks can split an SSE line anywhere; only complete lines are parsed.
        *lines, self._buffer = (self._buffer + chunk).split(b"\n")
        for line in lines:
            self._scan_line(line)

    def _scan_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        data = line[len(b"data:") :].strip()
        if data == b"[DONE]":
            return
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta")
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                self.content_parts.append(content)


class ChatService:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def complete(self, body: dict[str, Any]) -> ChatResult:
        """Non-streaming chat completion. Never raises for client, config, or upstream failures."""
        prepared = self._prepare(body)
        if isinstance(prepared, ChatResult):
            return prepared
        route, endpoint = prepared
        payload = {**body, "model": route.model}
        payload.pop("stream", None)
        payload.pop("stream_options", None)

        response = await self._send(route, endpoint, post_chat_completions, payload)
        if isinstance(response, ChatResult):
            return response
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return _upstream_error(
                route,
                endpoint,
                502,
                f"Provider '{route.provider}' returned a response that is not a JSON object.",
                "invalid_upstream_response",
            )
        return ChatResult(response.status_code, data, route.provider, route.model)

    async def stream(self, body: dict[str, Any]) -> ChatResult | ChatStream:
        """Streaming chat completion.

        Failures before the provider starts streaming come back as a `ChatResult`, so the client
        still gets a proper HTTP error status. Never raises for client, config, or upstream
        failures.
        """
        prepared = self._prepare(body)
        if isinstance(prepared, ChatResult):
            return prepared
        route, endpoint = prepared
        client_options = body.get("stream_options")
        payload = {
            **body,
            "model": route.model,
            "stream": True,
            # Ask for the final usage chunk so token counts can be logged; harmless to clients.
            "stream_options": {
                **(client_options if isinstance(client_options, dict) else {}),
                "include_usage": True,
            },
        }

        response = await self._send(route, endpoint, open_chat_completions_stream, payload)
        if isinstance(response, ChatResult):
            return response
        return ChatStream(response, route, _secrets(endpoint))

    def _prepare(self, body: dict[str, Any]) -> tuple[Route, ProviderEndpoint] | ChatResult:
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return _error(400, "'model' is required and must be a string.", "invalid_request_error")
        try:
            route = resolve_route(model, self._settings)
        except ProviderNotConfiguredError as exc:
            # 400, not 5xx: retrying can't help, and the message tells the owner which key to set.
            return _error(400, str(exc), "invalid_request_error", "provider_not_configured")
        return route, self._settings.endpoint(route.provider)

    async def _send(
        self,
        route: Route,
        endpoint: ProviderEndpoint,
        send: Callable[
            [httpx.AsyncClient, ProviderEndpoint, dict[str, Any]], Awaitable[httpx.Response]
        ],
        payload: dict[str, Any],
    ) -> httpx.Response | ChatResult:
        """Call upstream; return the response only if it succeeded (a stream's body stays open)."""
        try:
            response = await send(self._http, endpoint, payload)
        except httpx.TimeoutException:
            return _upstream_error(
                route, endpoint, 504, f"Provider '{route.provider}' timed out.", "upstream_timeout"
            )
        except httpx.RequestError as exc:
            # e.g. Ollama not running. The exception text names the URL, never headers.
            return _upstream_error(
                route,
                endpoint,
                502,
                f"Could not reach provider '{route.provider}': {type(exc).__name__}: {exc}",
                "upstream_unavailable",
            )
        if response.is_success:
            return response

        try:
            await response.aread()
        except httpx.HTTPError:
            pass  # the status alone is still worth reporting
        finally:
            await response.aclose()
        # Keep the provider's status so clients see e.g. 429 and back off, but wrap the message:
        # providers disagree on error body shape (Gemini sometimes sends a list).
        return _upstream_error(
            route,
            endpoint,
            response.status_code,
            f"Provider '{route.provider}' returned HTTP {response.status_code}: "
            f"{_upstream_message(response)}",
        )


def _error(
    status: int,
    message: str,
    error_type: str,
    code: str | None = None,
    provider: Provider | None = None,
    model: str | None = None,
) -> ChatResult:
    body = {"error": {"message": message, "type": error_type, "code": code}}
    return ChatResult(status, body, provider, model, message)


def _upstream_error(
    route: Route, endpoint: ProviderEndpoint, status: int, message: str, code: str | None = None
) -> ChatResult:
    message = _redact(message, _secrets(endpoint))
    return _error(status, message, "upstream_error", code, route.provider, route.model)


def _upstream_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except (ValueError, httpx.ResponseNotRead):
        data = None
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
    try:
        text = response.text
    except httpx.ResponseNotRead:
        text = ""
    return text[:MAX_UPSTREAM_ERROR_CHARS] or "(empty body)"


def _secrets(endpoint: ProviderEndpoint) -> list[str]:
    return [endpoint.api_key.get_secret_value()] if endpoint.api_key else []


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text
