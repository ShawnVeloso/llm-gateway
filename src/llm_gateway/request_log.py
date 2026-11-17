"""One SQLite row per request. `LoggedChat` wraps `CachedChat` with the same two calls, timing
each request and writing its row once the response (or stream) is finished. The same DB holds the
response cache; its schema is here too, in `MIGRATIONS`."""

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
from typing import Any, Literal

from llm_gateway.cache import USE_CACHE, CacheControl, CachedChat
from llm_gateway.service import Attempt, CacheStatus, ChatResult, ChatStream

logger = logging.getLogger(__name__)

CLIENT_DISCONNECTED = "Client disconnected before the stream finished."

# Schema changes, in order. The DB's `PRAGMA user_version` counts how many have been applied, so
# an existing log is upgraded in place at startup. Only ever append; never edit a shipped entry.
MIGRATIONS = [
    # 1 (Stage 1). IF NOT EXISTS: Stage 1 created this table without setting user_version.
    """
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,         -- UTC, ISO 8601, when the request arrived
        app TEXT NOT NULL,                -- X-App-Name header, or 'unknown'
        provider TEXT,                    -- who served it (or failed last); NULL if never routed
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
    );
    """,
    # 2 (Stage 2): retries and fallback. Rows from before this have NULL in the new columns.
    """
    ALTER TABLE requests ADD COLUMN requested_model TEXT;   -- as the client sent it
    ALTER TABLE requests ADD COLUMN attempt_count INTEGER;  -- upstream calls; 0 if never routed
    CREATE TABLE attempts (
        id INTEGER PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES requests (request_id),
        attempt_number INTEGER NOT NULL,  -- 1, 2, ... across retries and fallback
        provider TEXT NOT NULL,
        model TEXT NOT NULL,              -- name sent upstream
        status_code INTEGER NOT NULL,
        latency_ms REAL NOT NULL,         -- until the full response, or a stream's first bytes
        error_message TEXT                -- redacted
    );
    CREATE INDEX attempts_request_id ON attempts (request_id);
    """,
    # 3 (Stage 3): response cache. `cache_status` is NULL for older rows and while caching is off.
    """
    ALTER TABLE requests ADD COLUMN cache_status TEXT;  -- 'hit', 'miss' or 'bypass'
    CREATE TABLE cache (
        key TEXT PRIMARY KEY,         -- SHA-256 of the normalized request (cache.cache_key)
        created_at TEXT NOT NULL,     -- UTC, ISO 8601
        expires_at REAL NOT NULL,     -- Unix time; expired rows are ignored, deleted on writes
        provider TEXT NOT NULL,       -- who served the stored response
        model TEXT NOT NULL,          -- name sent upstream
        response BLOB NOT NULL        -- JSON body, or a stream's raw SSE bytes
    );
    CREATE INDEX cache_expires_at ON cache (expires_at);
    """,
]


INSERT = """
INSERT INTO requests (
    request_id, created_at, app, provider, model, requested_model, is_stream, input_tokens,
    output_tokens, latency_ms, ttft_ms, status_code, error_message, attempt_count, cache_status,
    request_content, response_content
) VALUES (
    :request_id, :created_at, :app, :provider, :model, :requested_model, :is_stream, :input_tokens,
    :output_tokens, :latency_ms, :ttft_ms, :status_code, :error_message, :attempt_count,
    :cache_status, :request_content, :response_content
)
"""

INSERT_ATTEMPT = """
INSERT INTO attempts (
    request_id, attempt_number, provider, model, status_code, latency_ms, error_message
) VALUES (
    :request_id, :attempt_number, :provider, :model, :status_code, :latency_ms, :error_message
)
"""


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    created_at: str
    app: str
    provider: str | None
    model: str | None
    requested_model: str | None
    is_stream: bool
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float
    ttft_ms: float | None
    status_code: int
    error_message: str | None
    cache_status: CacheStatus | None
    request_content: str | None
    response_content: str | None
    attempts: tuple[Attempt, ...]  # written to the `attempts` table; the count to `requests`


class RequestLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._pending: set[asyncio.Task[None]] = set()

    def create(self) -> None:
        """Create the DB file and bring its schema up to date. Blocking; call once at startup."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            # WAL lets a dashboard read the DB while the gateway is writing to it.
            db.execute("PRAGMA journal_mode=WAL")
            [version] = db.execute("PRAGMA user_version").fetchone()
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                # One transaction per migration, version bump included: a failure leaves the DB
                # as it was (the connection closes without COMMIT, which rolls back).
                db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")

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
        row = {name: getattr(record, name) for name in RequestRecord.__dataclass_fields__}
        row["attempt_count"] = len(record.attempts)
        attempts = [
            {"request_id": record.request_id, "attempt_number": number, **asdict(attempt)}
            for number, attempt in enumerate(record.attempts, start=1)
        ]
        # One transaction, so a request never appears without its attempts.
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute(INSERT, row)
            db.executemany(INSERT_ATTEMPT, attempts)

    def rows(self, table: Literal["requests", "attempts"] = "requests") -> list[dict[str, Any]]:
        """All rows of a table, oldest first. Blocking; for tests and quick checks."""
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            query = f"SELECT * FROM {table} ORDER BY id"  # noqa: S608 - table is one of two names
            return [dict(row) for row in db.execute(query)]


class LoggedChat:
    def __init__(self, chat: CachedChat, log: RequestLog, log_content: bool) -> None:
        self._chat = chat
        self._log = log
        self._log_content = log_content

    async def complete(
        self, body: dict[str, Any], app: str, control: CacheControl = USE_CACHE
    ) -> ChatResult:
        start = _Start.now()
        result = await self._chat.complete(body, control)
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

    async def stream(
        self, body: dict[str, Any], app: str, control: CacheControl = USE_CACHE
    ) -> "ChatResult | LoggedStream":
        start = _Start.now()
        stream = await self._chat.stream(body, control)
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
        result = ChatResult(
            200, {}, stream.provider, stream.model, error, stream.attempts, stream.cache_status
        )
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
            requested_model=requested if isinstance(requested, str) else None,
            is_stream=is_stream,
            input_tokens=_token_count(usage, "prompt_tokens"),
            output_tokens=_token_count(usage, "completion_tokens"),
            latency_ms=(time.perf_counter() - start.perf) * 1000,
            ttft_ms=ttft_ms,
            status_code=result.status_code,
            error_message=result.error_message,
            cache_status=result.cache_status,
            request_content=json.dumps(body.get("messages")) if self._log_content else None,
            response_content=(response_text or None) if self._log_content else None,
            attempts=result.attempts,
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

    @property
    def cache_status(self) -> CacheStatus | None:
        return self._stream.cache_status

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
