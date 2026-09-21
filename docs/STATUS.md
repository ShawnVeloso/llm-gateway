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
- Live-checked the not-configured path: with no `GEMINI_API_KEY`, `gemini-2.5-flash` (normal + streaming) returns 400 `provider_not_configured`.

## Next step
Last Stage 1 item: owner adds `GEMINI_API_KEY` to `.env`, then live curl `gemini-2.5-flash` normal + streaming through the gateway and confirm both rows (tokens, TTFT) in `data/gateway.db`. Then open the Stage 1 PR.

## Open questions
- Does Lithe call `GET /v1/models`? (Not in Stage 1 unless needed.)

## Known issues
- Live Gemini check blocked on a missing key (see Next step); Gemini's OpenAI-compat endpoint is beta and its streaming usage chunk / error shape are not fully documented.
- Anthropic's OpenAI-compat layer is not production-grade and ignores some fields (decision 0004).
- Requests rejected before the service (invalid JSON body) are not logged.
- Don't replace `SSEResponse`'s `finally` close with a `BackgroundTask`, or the shielded write with plain `to_thread`: both lose rows on client disconnect (decision 0005).
