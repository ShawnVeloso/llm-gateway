# Status

## Current stage
Stage 1 - pass-through + logging, on branch `feature/stage-1-server`. Stage 0 merged (PRs #1, #2); config, routing, service merged (PR #3).

## Done
- `config.py` Settings (fixed 127.0.0.1 bind) + `.env.example`; `routing.py` model -> provider via `provider/model` or longest prefix (decision 0004).
- `providers.py` + `service.py` `ChatService`: `complete` (non-streaming) and `stream` (bytes relayed unchanged, `include_usage` added, usage/TTFT/mid-stream error captured). All failures become OpenAI-style errors with keys redacted.
- `app.py`: `GET /health`, `POST /v1/chat/completions`, shared `httpx.AsyncClient`; `uv run llm-gateway` serves on 127.0.0.1.
- `request_log.py`: `LoggedChat` wraps `ChatService`, one `requests` row per chat request (app from `X-App-Name`, tokens only if reported, latency, TTFT, status, redacted error, content only with `LOG_CONTENT=true`). Stream rows written on end or client disconnect (decision 0005).
- Live-checked with Ollama: normal, streaming, long stream, curl disconnect mid-stream; rows correct.
- README: setup, run, client setup, request log, empty Results section.
- Live-checked with Gemini (`gemini-3.6-flash`): normal + streaming 200 with tokens and TTFT logged; no key -> 400 `provider_not_configured`; retired model -> upstream 404 passed through and logged; no key strings in the DB. Stage 1 acceptance met.

## Next step
Owner opens and merges the Stage 1 PR from `feature/stage-1-server`. Then Stage 2 (retries + fallback), see `docs/ROADMAP.md`.

## Open questions
- Does Lithe call `GET /v1/models`? (Not in Stage 1 unless needed.)

## Known issues
- Gemini's OpenAI-compat endpoint is beta. Observed: it puts `usage` on every stream chunk (no separate final chunk), and `total_tokens` includes thinking tokens that `completion_tokens` does not (e.g. 5 out, 296 total). We store only input/output, so thinking tokens are not logged yet; matters for Stage 4 cost.
- `gemini-2.5-flash` is no longer available to new keys; use `gemini-3.6-flash`.
- Anthropic's OpenAI-compat layer is not production-grade and ignores some fields (decision 0004).
- Requests rejected before the service (invalid JSON body) are not logged.
- Don't replace `SSEResponse`'s `finally` close with a `BackgroundTask`, or the shielded write with plain `to_thread`: both lose rows on client disconnect (decision 0005).
