# 0005: Request logging as a wrapper, safe on client disconnect

- **Status:** accepted
- **Stage:** 1

## Context
Every chat request needs one SQLite row without blocking the event loop. Streams only know their tokens and latency when they end, and live testing showed that a client hanging up mid-stream lost the row: Starlette skips `background` tasks on disconnect, and cancelling the request could drop an insert still queued for a worker thread.

## Decision
- `LoggedChat` wraps `ChatService` (same calls plus app name); `ChatService` stays logging-free.
- `RequestLog` opens one SQLite connection per insert inside `asyncio.to_thread`, WAL mode, failures only logged.
- Writes run as their own task behind `asyncio.shield`; pending writes are awaited at shutdown.
- `SSEResponse` closes the stream in `finally`; the row is written once, marked "Client disconnected..." if unfinished.

## Alternatives
- Logging inside `ChatService` - mixes concerns; Stage 2 retry/fallback wraps the same call.
- One shared connection or a writer queue - more moving parts than a local gateway needs.
- `BackgroundTask` for stream cleanup - skipped on disconnect.

## Why
A wrapper keeps each layer testable. A connection per write is cheap next to an LLM call. The shield and `finally` are what make disconnected streams show up in usage data, which Stage 4 relies on.
