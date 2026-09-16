"""One SQLite row per request. `LoggedChat` wraps `ChatService` with the same two calls, timing
each request and writing its row once the response (or stream) is finished."""

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_gateway.service import ChatResult, ChatService, ChatStream

logger = logging.getLogger(__name__)

CLIENT_DISCONNECTED = "Client disconnected before the stream finished."

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,         -- UTC, ISO 8601, when the request arrived
    app TEXT NOT NULL,                -- X-App-Name header, or 'unknown'
    provider TEXT,                    -- NULL if the request failed before routing
    model TEXT,                       -- name sent upstream, else the name the client asked for
    is_stream INTEGER NOT NULL,
    input_tokens INTEGER,             -- only as reported by the provider, never estimated
    output_tokens INTEGER,
    latency_ms REAL NOT NULL,         -- until the last byte (streams) or the full response
    ttft_ms REAL,                     -- streams only: until the first bytes arrived
    status_code INTEGER NOT NULL,
    error_message TEXT,               -- redacted
    request_content TEXT,             -- JSON messages; only when LOG_CONTENT=true
    response_content TEXT             -- assistant text; only when LOG_CONTENT=true
)
"""


INSERT = """
INSERT INTO requests (
    request_id, created_at, app, provider, model, is_stream, input_tokens, output_tokens,
    latency_ms, ttft_ms, status_code, error_message, request_content, response_content
) VALUES (
    :request_id, :created_at, :app, :provider, :model, :is_stream, :input_tokens, :output_tokens,
    :latency_ms, :ttft_ms, :status_code, :error_message, :request_content, :response_content
)
"""


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    created_at: str
    app: str
    provider: str | None
    model: str | None
    is_stream: bool
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float
    ttft_ms: float | None
    status_code: int
    error_message: str | None
    request_content: str | None
    response_content: str | None


class RequestLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._pending: set[asyncio.Task[None]] = set()

    def create(self) -> None:
        """Create the DB file and table if missing. Blocking; call once at startup."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            # WAL lets a dashboard read the DB while the gateway is writing to it.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(SCHEMA)

    async def write(self, record: RequestRecord) -> None:
        """Insert a row off the event loop. A logging failure never fails the request.

        The insert finishes even if the caller is cancelled (the server cancels the request when a
        client disconnects mid-stream). Without the shield, cancelling can drop an insert that is
        still queued for a worker thread.
        """
        task = asyncio.create_task(self._write(record))
        self._pending.add(task)  # the loop only holds weak references to tasks
        task.add_done_callback(self._pending.discard)
        await asyncio.shield(task)

    async def wait_for_pending_writes(self) -> None:
        await asyncio.gather(*self._pending)

    async def _write(self, record: RequestRecord) -> None:
        try:
            await asyncio.to_thread(self._insert, record)
        except sqlite3.Error:
            logger.exception("Could not write request %s to the log DB", record.request_id)

    def _insert(self, record: RequestRecord) -> None:
        # A connection per write: SQLite connections can't be shared across to_thread's threads,
        # and opening a local file is cheap next to an LLM call.
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute(INSERT, asdict(record))

    def rows(self) -> list[dict[str, Any]]:
        """All rows, oldest first. Blocking; for tests and quick checks."""
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY id")]


class LoggedChat:
    def __init__(self, chat: ChatService, log: RequestLog, log_content: bool) -> None:
        self._chat = chat
        self._log = log
        self._log_content = log_content

    async def complete(self, body: dict[str, Any], app: str) -> ChatResult:
        start = _Start.now()
        result = await self._chat.complete(body)
        usage = result.body.get("usage")
        await self._log.write(
            self._record(
                start,
                app,
                body,
                result,
                is_stream=False,
                usage=usage if isinstance(usage, dict) else None,
                response_text=_message_text(result.body),
            )
        )
        return result

    async def stream(self, body: dict[str, Any], app: str) -> "ChatResult | LoggedStream":
        start = _Start.now()
        stream = await self._chat.stream(body)
        if isinstance(stream, ChatResult):
            await self._log.write(
                self._record(start, app, body, stream, is_stream=True, usage=None)
            )
            return stream
        return LoggedStream(
            stream,
            lambda finished: self._stream_record(start, app, body, stream, finished),
            self._log,
        )

    def _stream_record(
        self, start: "_Start", app: str, body: dict[str, Any], stream: ChatStream, finished: bool
    ) -> RequestRecord:
        error = stream.error_message
        if error is None and not finished:
            error = CLIENT_DISCONNECTED
        # 200 because that's the status the client already received.
        result = ChatResult(200, {}, stream.provider, stream.model, error)
        ttft = None
        if stream.first_chunk_at is not None:
            ttft = (stream.first_chunk_at - start.perf) * 1000
        return self._record(
            start,
            app,
            body,
            result,
            is_stream=True,
            usage=stream.usage,
            ttft_ms=ttft,
            response_text="".join(stream.content_parts),
        )

    def _record(
        self,
        start: "_Start",
        app: str,
        body: dict[str, Any],
        result: ChatResult,
        *,
        is_stream: bool,
        usage: dict[str, Any] | None,
        ttft_ms: float | None = None,
        response_text: str | None = None,
    ) -> RequestRecord:
        requested = body.get("model")
        return RequestRecord(
            request_id=uuid.uuid4().hex,
            created_at=start.wall,
            app=app,
            provider=result.provider,
            model=result.model or (requested if isinstance(requested, str) else None),
            is_stream=is_stream,
            input_tokens=_token_count(usage, "prompt_tokens"),
            output_tokens=_token_count(usage, "completion_tokens"),
            latency_ms=(time.perf_counter() - start.perf) * 1000,
            ttft_ms=ttft_ms,
            status_code=result.status_code,
            error_message=result.error_message,
            request_content=json.dumps(body.get("messages")) if self._log_content else None,
            response_content=(response_text or None) if self._log_content else None,
        )


class LoggedStream:
    """Relays a `ChatStream` and writes its row exactly once, when it ends or is closed."""

    def __init__(
        self, stream: ChatStream, make_record: Callable[[bool], RequestRecord], log: RequestLog
    ) -> None:
        self._stream = stream
        self._make_record = make_record  # called with whether the stream ran to its end
        self._log = log
        self._finished = False
        self._closed = False

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream.chunks():
                yield chunk
            self._finished = True
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Close upstream and write the row. Safe to call more than once; only the first logs.

        Must be called even if `chunks()` was never finished: on a client disconnect the server
        abandons the generator without closing it.
        """
        if self._closed:
            return
        self._closed = True
        record = self._make_record(self._finished)
        try:
            await self._stream.aclose()
        finally:
            await self._log.write(record)


@dataclass(frozen=True)
class _Start:
    wall: str
    perf: float

    @classmethod
    def now(cls) -> "_Start":
        wall = datetime.now(UTC).isoformat(timespec="milliseconds")
        return cls(wall, time.perf_counter())


def _token_count(usage: dict[str, Any] | None, key: str) -> int | None:
    value = usage.get(key) if usage else None
    # bool is a subclass of int; a provider sending true/false is not a count.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _message_text(body: dict[str, Any]) -> str | None:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else None
