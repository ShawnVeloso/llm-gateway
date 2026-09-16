"""FastAPI app: thin HTTP routes over `ChatService`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from llm_gateway.config import Settings, get_settings
from llm_gateway.service import ChatService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One client for the whole app so connections to providers are reused across requests.
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as http:
            app.state.chat = ChatService(settings, http)
            yield

    app = FastAPI(title="llm-gateway", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        try:
            body: Any = await request.json()
        except ValueError:
            return _error(400, "Request body must be valid JSON.", "invalid_request_error")
        if not isinstance(body, dict):
            return _error(400, "Request body must be a JSON object.", "invalid_request_error")
        if body.get("stream") is True:
            # Temporary until streaming lands: a JSON reply would break a client expecting SSE.
            return _error(
                400,
                "Streaming is not supported by the gateway yet.",
                "invalid_request_error",
                "streaming_not_supported",
            )

        chat: ChatService = request.app.state.chat
        result = await chat.complete(body)
        return JSONResponse(result.body, status_code=result.status_code)

    return app


def _error(status: int, message: str, error_type: str, code: str | None = None) -> JSONResponse:
    body = {"error": {"message": message, "type": error_type, "code": code}}
    return JSONResponse(body, status_code=status)
