"""Request logic behind `/v1/chat/completions`: route, forward with retries and fallback, and turn
every failure into an OpenAI-style error. Logging (`request_log.LoggedChat`) and later stages
(cache) wrap `ChatService.complete` and `ChatService.stream`."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx

from llm_gateway.config import Provider, ProviderEndpoint, Settings
from llm_gateway.providers import open_chat_completions_stream, post_chat_completions
from llm_gateway.retry import backoff_seconds, is_retryable_status, parse_retry_after
from llm_gateway.routing import ProviderNotConfiguredError, Route, resolve_route

logger = logging.getLogger(__name__)

MAX_UPSTREAM_ERROR_CHARS = 500


@dataclass(frozen=True)
class Attempt:
    """One call to a provider. A request makes several when it retries or falls back."""

    provider: Provider
    model: str
    status_code: int
    latency_ms: float  # until the full response, or a stream's first bytes
    error_message: str | None = None  # redacted


@dataclass(frozen=True)
class ChatResult:
    status_code: int
    body: dict[str, Any]
    provider: Provider | None = None  # None when the request failed before routing
    model: str | None = None  # name sent upstream
    error_message: str | None = None  # already redacted; safe to log
    attempts: tuple[Attempt, ...] = ()


class ChatStream:
    """An open upstream SSE stream. Iterate `chunks()` to relay it; read the stats afterwards."""

    def __init__(self, response: httpx.Response, route: Route, secrets: list[str]) -> None:
        self.status_code = response.status_code
        self.provider: Provider = route.provider
        self.model = route.model
        self.attempts: tuple[Attempt, ...] = ()  # every try, including the one serving this
        self.usage: dict[str, Any] | None = None  # from the provider's usage chunk, if it sent one
        self.first_chunk_at: float | None = None  # time.perf_counter() of the first body bytes
        self.error_message: str | None = None  # set if the stream broke part-way; redacted
        self.content_parts: list[str] = []  # assistant text deltas, in order
        self._response = response
        self._secrets = secrets
        self._body = response.aiter_bytes()
        self._first: bytes | None = None
        self._buffer = b""

    async def start(self) -> None:
        """Wait for the first bytes. Raises httpx errors if the stream breaks before any arrive.

        Nothing has been sent to the client yet at that point, so the request can still be
        retried or fall back. After the first bytes, it can't.
        """
        try:
            self._first = await anext(self._body)
        except StopAsyncIteration:
            return  # an empty body; `chunks()` yields nothing
        self.first_chunk_at = time.perf_counter()
        self._scan(self._first)

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield the provider's bytes unchanged, as they arrive. Call `start()` first."""
        try:
            if self._first:
                yield self._first
            async for chunk in self._body:
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


@dataclass(frozen=True)
class _Failure:
    """A failed attempt, and whether trying again (or elsewhere) could help."""

    result: ChatResult
    retryable: bool
    retry_after: float | None = None  # seconds, from the provider's Retry-After header


class ChatService:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def complete(self, body: dict[str, Any]) -> ChatResult:
        """Non-streaming chat completion. Never raises for client, config, or upstream failures."""

        async def attempt(route: Route, endpoint: ProviderEndpoint) -> ChatResult | _Failure:
            payload = {**body, "model": route.model}
            payload.pop("stream", None)
            payload.pop("stream_options", None)
            response = await self._send(route, endpoint, post_chat_completions, payload)
            if isinstance(response, _Failure):
                return response
            try:
                data = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return _Failure(
                    _upstream_error(
                        route,
                        endpoint,
                        502,
                        f"Provider '{route.provider}' returned a response that is not a JSON "
                        "object.",
                        "invalid_upstream_response",
                    ),
                    retryable=False,
                )
            return ChatResult(response.status_code, data, route.provider, route.model)

        outcome = await self._run(body, attempt)
        if isinstance(outcome, ChatResult):
            return outcome
        result, attempts = outcome
        return replace(result, attempts=attempts)

    async def stream(self, body: dict[str, Any]) -> ChatResult | ChatStream:
        """Streaming chat completion.

        Failures before the provider sends its first bytes come back as a `ChatResult`, so the
        client still gets a proper HTTP error status. Never raises for client, config, or
        upstream failures.
        """
        client_options = body.get("stream_options")

        async def attempt(route: Route, endpoint: ProviderEndpoint) -> ChatStream | _Failure:
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
            if isinstance(response, _Failure):
                return response
            stream = ChatStream(response, route, _secrets(endpoint))
            try:
                await stream.start()
            except httpx.HTTPError as exc:
                await stream.aclose()
                return _transport_failure(route, endpoint, exc)
            return stream

        outcome = await self._run(body, attempt)
        if isinstance(outcome, ChatResult):
            return outcome
        stream, attempts = outcome
        stream.attempts = attempts
        return stream

    async def _run[T: ChatResult | ChatStream](
        self,
        body: dict[str, Any],
        attempt: Callable[[Route, ProviderEndpoint], Awaitable[T | _Failure]],
    ) -> tuple[T, tuple[Attempt, ...]] | ChatResult:
        """Try the model's provider with retries, then its fallback. Returns the first success
        with every attempt made, or the last failure (with the same) as a `ChatResult`."""
        routes = self._routes(body)
        if isinstance(routes, ChatResult):
            return routes
        attempts: list[Attempt] = []
        failure: _Failure | None = None
        for route in routes:
            endpoint = self._settings.endpoint(route.provider)
            for retry in range(self._settings.max_retries + 1):
                start = time.perf_counter()
                outcome = await attempt(route, endpoint)
                latency_ms = (time.perf_counter() - start) * 1000
                if not isinstance(outcome, _Failure):
                    attempts.append(
                        Attempt(route.provider, route.model, outcome.status_code, latency_ms)
                    )
                    return outcome, tuple(attempts)
                failure = outcome
                attempts.append(
                    Attempt(
                        route.provider,
                        route.model,
                        failure.result.status_code,
                        latency_ms,
                        failure.result.error_message,
                    )
                )
                if not failure.retryable:
                    return replace(failure.result, attempts=tuple(attempts))
                if retry == self._settings.max_retries:
                    break
                delay = backoff_seconds(
                    retry,
                    self._settings.retry_base_delay_seconds,
                    self._settings.retry_max_delay_seconds,
                    failure.retry_after,
                )
                if delay is None:
                    break  # the provider wants a longer pause than we'll make the client wait
                await asyncio.sleep(delay)
        # Every route made at least one attempt, so `failure` is set.
        return replace(cast(_Failure, failure).result, attempts=tuple(attempts))

    def _routes(self, body: dict[str, Any]) -> list[Route] | ChatResult:
        """The route for the client's model, then its fallback if one is configured."""
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return _error(400, "'model' is required and must be a string.", "invalid_request_error")
        try:
            route = resolve_route(model, self._settings)
        except ProviderNotConfiguredError as exc:
            # 400, not 5xx: retrying can't help, and the message tells the owner which key to set.
            # No fallback either: silently using another model would hide the missing key.
            return _error(400, str(exc), "invalid_request_error", "provider_not_configured")
        routes = [route]
        fallback_model = self._settings.fallback_models.get(route.provider)
        if fallback_model:
            try:
                fallback = resolve_route(fallback_model, self._settings)
            except ProviderNotConfiguredError as exc:
                logger.warning("Fallback for provider '%s' skipped: %s", route.provider, exc)
            else:
                if fallback != route:
                    routes.append(fallback)
        return routes

    async def _send(
        self,
        route: Route,
        endpoint: ProviderEndpoint,
        send: Callable[
            [httpx.AsyncClient, ProviderEndpoint, dict[str, Any]], Awaitable[httpx.Response]
        ],
        payload: dict[str, Any],
    ) -> httpx.Response | _Failure:
        """Call upstream; return the response only if it succeeded (a stream's body stays open)."""
        try:
            response = await send(self._http, endpoint, payload)
        except httpx.HTTPError as exc:
            return _transport_failure(route, endpoint, exc)
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
        result = _upstream_error(
            route,
            endpoint,
            response.status_code,
            f"Provider '{route.provider}' returned HTTP {response.status_code}: "
            f"{_upstream_message(response)}",
        )
        return _Failure(
            result,
            retryable=is_retryable_status(response.status_code),
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )


def _transport_failure(route: Route, endpoint: ProviderEndpoint, exc: httpx.HTTPError) -> _Failure:
    """No usable HTTP response: timed out, couldn't connect, or the connection broke."""
    if isinstance(exc, httpx.TimeoutException):
        result = _upstream_error(
            route, endpoint, 504, f"Provider '{route.provider}' timed out.", "upstream_timeout"
        )
    else:
        # e.g. Ollama not running. The exception text names the URL, never headers.
        result = _upstream_error(
            route,
            endpoint,
            502,
            f"Could not reach provider '{route.provider}': {type(exc).__name__}: {exc}",
            "upstream_unavailable",
        )
    return _Failure(result, retryable=True)


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
