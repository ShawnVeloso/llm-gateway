# Status

## Current stage
Stage 2 - retries + fallback, on branch `feature/stage-2-retries-fallback`. Stage 1 merged (PRs #3-#5).

## Done
- Stage 1: pass-through for Gemini/Ollama/OpenAI/Anthropic with routing (decision 0004), SSE streaming, one SQLite row per request (decision 0005). Live-checked with Ollama and Gemini.
- Stage 2 (decision 0006): `retry.py` policy (connect errors, timeouts, 429, 5xx; backoff with jitter; `Retry-After` up to the cap). `ChatService._run` retries per provider, then falls back via `FALLBACK_MODELS` (default gemini -> `ollama/qwen2.5`). Streams read the first chunk before relaying, so they can retry or fall back until the first byte.
- Log: `attempts` table (one row per upstream call) + `requested_model`, `attempt_count` on `requests`; schema versioned with `MIGRATIONS` / `PRAGMA user_version`. Upgrade tested on a copy of the real Stage 1 DB (rows kept).
- Tests cover retry-then-success, exhausted -> fallback, 4xx not retried, long Retry-After, stream fallback before the first byte and none after.
- Live-checked with Gemini made unreachable: normal + streaming fell back to qwen2.5, 200, 4 attempts logged each. Stage 2 acceptance met.

## Next step
Owner opens and merges the Stage 2 PR from `feature/stage-2-retries-fallback`. Then Stage 3 (response caching), see `docs/ROADMAP.md`.

## Open questions
- Does Lithe call `GET /v1/models`? (Not needed so far.)

## Known issues
- Gemini's OpenAI-compat endpoint is beta. Observed: it puts `usage` on every stream chunk (no separate final chunk), and `total_tokens` includes thinking tokens that `completion_tokens` does not (e.g. 5 out, 296 total). We store only input/output, so thinking tokens are not logged yet; matters for Stage 4 cost.
- `gemini-2.5-flash` is no longer available to new keys; use `gemini-3.6-flash`.
- Anthropic's OpenAI-compat layer is not production-grade and ignores some fields (decision 0004).
- Requests rejected before the service (invalid JSON body) are not logged.
- Don't replace `SSEResponse`'s `finally` close with a `BackgroundTask`, or the shielded write with plain `to_thread`: both lose rows on client disconnect (decision 0005).
- A first fallback to qwen2.5 can take ~100s while Ollama loads the model (observed). With the 120s timeout per attempt, a hanging provider can take minutes to give up (decision 0006).
- On Windows a refused local connection takes ~2s per attempt (OS-level SYN retries), so an unreachable Ollama costs ~6s before erroring.
