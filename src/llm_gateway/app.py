"""FastAPI app: thin HTTP routes over `ChatService`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from llm_gateway.config import Settings, get_settings
from llm_gateway.service import ChatResult, ChatService


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
        chat: ChatService = request.app.state.chat
        if body.get("stream") is not True:
            result = await chat.complete(body)
            return JSONResponse(result.body, status_code=result.status_code)

        stream = await chat.stream(body)
        if isinstance(stream, ChatResult):
            return JSONResponse(stream.body, status_code=stream.status_code)
        return StreamingResponse(
            stream.chunks(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
            # chunks() closes upstream when it ends; this also covers a client that disconnects
            # before the first chunk is read. Closing twice is harmless.
            background=BackgroundTask(stream.aclose),
        )

    return app


def _error(status: int, message: str, error_type: str, code: str | None = None) -> JSONResponse:
    body = {"error": {"message": message, "type": error_type, "code": code}}
    return JSONResponse(body, status_code=status)
