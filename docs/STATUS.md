# Status

## Current stage
Stage 1 - pass-through + logging, on branch `feature/stage-1-server`. Stage 0 merged (PRs #1, #2); config, routing, service merged (PR #3).

## Done
- uv project (`src/` layout), dependencies, ruff + pytest config.
- Context system: CLAUDE.md, STATUS, ROADMAP, decision records, settings (deny rules, ruff hook), `/handoff` skill, `test-runner` and `docs-researcher` subagents.
- Vendored skills: codebase-design, tdd, diagnosing-bugs, grilling/grill-me, resolving-merge-conflicts, frontend-design (see decision 0003).
- Stage 1: `config.py` (Settings via pydantic-settings, fixed 127.0.0.1 bind) + `.env.example` + tests.
- Stage 1: `routing.py` - model -> provider (Gemini/Ollama/OpenAI/Anthropic) via `provider/model` or longest prefix; missing key raises `ProviderNotConfiguredError` (decision 0004).
- Stage 1: `providers.py` (one httpx POST for all providers) + `service.py` `ChatService.complete` (non-streaming): routes, strips `stream` flags, maps not-configured (400), upstream HTTP errors (status kept, key redacted), connect errors (502), timeouts (504) to OpenAI-style errors. Returns `ChatResult`, never raises.
- Stage 1: `app.py` `create_app` - `GET /health`, `POST /v1/chat/completions`, one shared `httpx.AsyncClient` per app via lifespan; `uv run llm-gateway` serves it with uvicorn on 127.0.0.1.
- Stage 1: streaming - `ChatService.stream` returns a `ChatStream` (or a `ChatResult` if it fails before the first byte, so clients get a real HTTP error). Bytes relayed unchanged; gateway adds `stream_options.include_usage=true`; `ChatStream` records `usage`, `first_chunk_at`, and a mid-stream `error_message` (also sent to the client as an SSE error event). Live-checked with Ollama (normal + streaming, usage chunk present).
- Stage 1: SQLite logging - `request_log.py`: `RequestLog` (table `requests`, WAL, one connection per insert via `asyncio.to_thread`) and `LoggedChat`, which wraps `ChatService` and writes one row per chat request (app from `X-App-Name`, tokens only if reported, latency, TTFT, status, redacted error; prompt/response text only with `LOG_CONTENT=true`). Stream rows are written when the stream ends or the client disconnects (`error_message` = "Client disconnected..."). Live-checked with Ollama incl. a curl disconnect. Requests rejected before the service (invalid JSON body) are not logged.

## Next step
Stage 1: README (run + client setup, empty Results section), then live Gemini check (normal + streaming, rows appear in DB).

## Open questions
- Does Lithe call `GET /v1/models`? (Not in Stage 1 unless needed.)

## Known issues
- Client disconnect mid-stream: Starlette skips `background` tasks and uvicorn cancels the request, so `SSEResponse` closes the stream in `finally` and `RequestLog.write` shields the insert. Don't swap these back to a plain `BackgroundTask`/`to_thread`.
- Gemini's OpenAI-compatible endpoint is beta; streaming usage chunk and error body shape are not fully documented. Confirm with live curl at end of Stage 1.
- Anthropic's OpenAI-compat layer is not production-grade and ignores some fields (see decision 0004).
