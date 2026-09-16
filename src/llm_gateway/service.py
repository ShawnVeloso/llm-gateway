"""Request logic behind `/v1/chat/completions`: route, forward, and turn every failure into an
OpenAI-style error. Later stages (retry, fallback, cache, logging) wrap `ChatService.complete`."""

from dataclasses import dataclass
from typing import Any

import httpx

from llm_gateway.config import Provider, Settings
from llm_gateway.providers import post_chat_completions
from llm_gateway.routing import ProviderNotConfiguredError, resolve_route

MAX_UPSTREAM_ERROR_CHARS = 500


@dataclass(frozen=True)
class ChatResult:
    status_code: int
    body: dict[str, Any]
    provider: Provider | None = None  # None when the request failed before routing
    model: str | None = None  # name sent upstream
    error_message: str | None = None  # already redacted; safe to log


class ChatService:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def complete(self, body: dict[str, Any]) -> ChatResult:
        """Non-streaming chat completion. Never raises for client, config, or upstream failures."""
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return _error(400, "'model' is required and must be a string.", "invalid_request_error")

        try:
            route = resolve_route(model, self._settings)
        except ProviderNotConfiguredError as exc:
            # 400, not 5xx: retrying can't help, and the message tells the owner which key to set.
            return _error(400, str(exc), "invalid_request_error", "provider_not_configured")

        endpoint = self._settings.endpoint(route.provider)
        payload = {**body, "model": route.model}
        payload.pop("stream", None)
        payload.pop("stream_options", None)

        def fail(status: int, message: str, error_type: str, code: str | None = None) -> ChatResult:
            secrets = [endpoint.api_key.get_secret_value()] if endpoint.api_key else []
            return _error(
                status, _redact(message, secrets), error_type, code, route.provider, route.model
            )

        try:
            response = await post_chat_completions(self._http, endpoint, payload)
        except httpx.TimeoutException:
            return fail(
                504, f"Provider '{route.provider}' timed out.", "upstream_error", "upstream_timeout"
            )
        except httpx.RequestError as exc:
            # e.g. Ollama not running. The exception text names the URL, never headers.
            return fail(
                502,
                f"Could not reach provider '{route.provider}': {type(exc).__name__}: {exc}",
                "upstream_error",
                "upstream_unavailable",
            )

        if response.is_success:
            try:
                data = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return fail(
                    502,
                    f"Provider '{route.provider}' returned a response that is not a JSON object.",
                    "upstream_error",
                    "invalid_upstream_response",
                )
            return ChatResult(response.status_code, data, route.provider, route.model)

        # Keep the provider's status so clients see e.g. 429 and back off, but wrap the message:
        # providers disagree on error body shape (Gemini sometimes sends a list).
        return fail(
            response.status_code,
            f"Provider '{route.provider}' returned HTTP {response.status_code}: "
            f"{_upstream_message(response)}",
            "upstream_error",
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


def _upstream_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
    return response.text[:MAX_UPSTREAM_ERROR_CHARS] or "(empty body)"


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text
