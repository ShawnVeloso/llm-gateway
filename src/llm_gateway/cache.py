"""Exact-match response cache (decision 0007). `CachedChat` wraps `ChatService` with the same two
calls: a hit is answered from the `cache` table without calling a provider, and a clean success
from the requested model (not a fallback) is stored for `CACHE_TTL_SECONDS`."""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from llm_gateway.config import Provider
from llm_gateway.routing import Route
from llm_gateway.service import Attempt, CacheStatus, ChatResult, ChatService, ChatStream

logger = logging.getLogger(__name__)

# Stream flags don't change the answer. The stream flag still goes into the key, because a
# stream is stored as SSE bytes and a normal response as JSON.
_NOT_IN_KEY = ("stream", "stream_options")


def cache_key(body: dict[str, Any], is_stream: bool) -> str:
    """SHA-256 of the request with key order and whitespace normalized away.

    Any other difference (a message, temperature, even `0` vs `0.0`) is a different key: a cache
    that sometimes returns the answer to a slightly different question would be worse than none.
    """
    request = {name: value for name, value in body.items() if name not in _NOT_IN_KEY}
    canonical = json.dumps(
        [is_stream, request], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class CacheControl:
    """What the client allows, from its `Cache-Control` request header."""

    read: bool = True  # may be answered from the cache
    write: bool = True  # its response may be stored

    @classmethod
    def from_header(cls, value: str | None) -> "CacheControl":
        # Standard HTTP meaning: no-cache = get a fresh answer (which may still be stored, so the
        # next request gets it); no-store = don't keep anything either.
        directives = {part.split("=")[0].strip().lower() for part in (value or "").split(",")}
        if "no-store" in directives:
            return cls(read=False, write=False)
        if "no-cache" in directives:
            return cls(read=False)
        return cls()


USE_CACHE = CacheControl()


@dataclass(frozen=True)
class CacheEntry:
    provider: Provider
    model: str  # name sent upstream
    response: bytes  # JSON body, or a stream's raw SSE bytes


class ResponseCache:
    """The `cache` table in the gateway DB (created by `request_log.MIGRATIONS`). A cache failure
    is logged and treated as a miss; it never fails the request."""

    def __init__(self, path: Path, ttl_seconds: float) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds

    async def get(self, key: str) -> CacheEntry | None:
        try:
            return await asyncio.to_thread(self._get, key)
        except sqlite3.Error:
            logger.exception("Could not read the response cache")
            return None

    async def put(self, key: str, entry: CacheEntry) -> None:
        try:
            await asyncio.to_thread(self._put, key, entry)
        except sqlite3.Error:
            logger.exception("Could not write to the response cache")

    def _get(self, key: str) -> CacheEntry | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute(
                "SELECT provider, model, response FROM cache WHERE key = ? AND expires_at > ?",
                (key, time.time()),
            ).fetchone()
        return CacheEntry(cast(Provider, row[0]), row[1], row[2]) if row else None

    def _put(self, key: str, entry: CacheEntry) -> None:
        now = time.time()
        created_at = datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            # Clearing expired rows here keeps the table from growing without a background job.
            db.execute("DELETE FROM cache WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT OR REPLACE INTO cache (key, created_at, expires_at, provider, model, "
                "response) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    created_at,
                    now + self.ttl_seconds,
                    entry.provider,
                    entry.model,
                    entry.response,
                ),
            )


class CachedChat:
    def __init__(self, chat: ChatService, cache: ResponseCache | None) -> None:
        self._chat = chat
        self._cache = cache  # None: caching is off, every request goes through untouched

    async def complete(self, body: dict[str, Any], control: CacheControl = USE_CACHE) -> ChatResult:
        cache = self._cache
        if cache is None:
            return await self._chat.complete(body)
        key = cache_key(body, is_stream=False)
        if control.read and (entry := await cache.get(key)):
            data = json.loads(entry.response)
            return ChatResult(200, data, entry.provider, entry.model, cache_status="hit")

        result = await self._chat.complete(body)
        if control.write and result.status_code == 200 and _served_as_requested(result.attempts):
            response = json.dumps(result.body).encode()
            await cache.put(
                key, CacheEntry(cast(Provider, result.provider), result.model, response)
            )
        return replace(result, cache_status=_status(control))

    async def stream(
        self, body: dict[str, Any], control: CacheControl = USE_CACHE
    ) -> ChatResult | ChatStream:
        cache = self._cache
        if cache is None:
            return await self._chat.stream(body)
        key = cache_key(body, is_stream=True)
        if control.read and (entry := await cache.get(key)):
            # Replay through the same class as a live stream, so logging reads usage and text
            # from it the same way.
            replay = httpx.Response(200, content=entry.response)
            stream = ChatStream(replay, Route(entry.provider, entry.model), secrets=[])
            await stream.start()
            stream.cache_status = "hit"
            return stream

        stream = await self._chat.stream(body)
        if isinstance(stream, ChatResult):
            return replace(stream, cache_status=_status(control))
        stream.cache_status = _status(control)
        if control.write and _served_as_requested(stream.attempts):
            live = stream

            async def store(data: bytes) -> None:
                await cache.put(key, CacheEntry(live.provider, live.model, data))

            stream.on_complete = store
        return stream


def _status(control: CacheControl) -> CacheStatus:
    return "miss" if control.read else "bypass"


def _served_as_requested(attempts: tuple[Attempt, ...]) -> bool:
    """False if a fallback model answered. Caching that would keep serving the fallback's answer
    for the whole TTL, even after the requested provider is back."""
    first, last = attempts[0], attempts[-1]
    return (first.provider, first.model) == (last.provider, last.model)
