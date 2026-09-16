"""FastAPI app: thin HTTP routes over `ChatService`."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from llm_gateway.config import Settings, get_settings
from llm_gateway.request_log import LoggedChat, LoggedStream, RequestLog
from llm_gateway.service import ChatResult, ChatService

DEFAULT_APP_NAME = "unknown"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log = RequestLog(settings.db_path)
        await asyncio.to_thread(log.create)
        # One client for the whole app so connections to providers are reused across requests.
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as http:
            app.state.chat = LoggedChat(ChatService(settings, http), log, settings.log_content)
            yield
        await log.wait_for_pending_writes()

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
        chat: LoggedChat = request.app.state.chat
        app_name = request.headers.get("X-App-Name") or DEFAULT_APP_NAME
        if body.get("stream") is not True:
            result = await chat.complete(body, app_name)
            return JSONResponse(result.body, status_code=result.status_code)

        stream = await chat.stream(body, app_name)
        if isinstance(stream, ChatResult):
            return JSONResponse(stream.body, status_code=stream.status_code)
        return SSEResponse(stream)

    return app


class SSEResponse(StreamingResponse):
    """Relays a `LoggedStream` and always closes it when the response ends.

    A plain `background` task is not enough: when the client disconnects mid-stream, Starlette
    skips it (it raises `ClientDisconnect` or cancels the send loop, depending on the server),
    leaving upstream open and the request unlogged.
    """

    def __init__(self, stream: LoggedStream) -> None:
        super().__init__(
            stream.chunks(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )
        self._stream = stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._stream.aclose()


def _error(status: int, message: str, error_type: str, code: str | None = None) -> JSONResponse:
    body = {"error": {"message": message, "type": error_type, "code": code}}
    return JSONResponse(body, status_code=status)
