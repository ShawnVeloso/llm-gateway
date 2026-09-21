# Status

## Current stage
Stage 3 - response caching, on branch `feature/stage-3-response-cache`. Stages 1-2 merged (PRs #3-#6).

## Done
- Stage 1: pass-through for Gemini/Ollama/OpenAI/Anthropic with routing (decision 0004), SSE streaming, one SQLite row per request (decision 0005). Live-checked with Ollama and Gemini.
- Stage 2 (decision 0006): retries with backoff, then fallback via `FALLBACK_MODELS`; `attempts` table; schema versioned with `MIGRATIONS` / `PRAGMA user_version`.
- Stage 3 (decision 0007): `cache.py` exact-match cache in a `cache` table (migration 3), TTL `CACHE_TTL_SECONDS` (1h), on by default. `Cache-Control: no-cache` / `no-store` bypass; `X-Cache` response header; `requests.cache_status`. Streams stored as raw SSE bytes if they ended cleanly and replayed on a hit. Errors and fallback answers are never cached.
- Tests cover key normalization, hit/miss for normal and streaming requests, both bypass headers, TTL expiry, and that errors, broken streams and fallback answers aren't cached. The last two were checked by breaking the code on purpose.
- Live-checked with Ollama llama3.2 (fresh DB): normal 27.3s miss (model loading) -> 4.7ms hit; stream 125ms -> 4.5ms; no-cache -> BYPASS. Stage 3 acceptance met. Not yet run against the real `data/gateway.db` (access is blocked for Claude); migration 3 runs on first start.

## Next step
Push `feature/stage-3-response-cache`; owner opens and merges the PR. Then Stage 4 (usage dashboard), see `docs/ROADMAP.md`.

## Open questions
- Does Lithe call `GET /v1/models`? (Not needed so far.)
- Can Lithe send `Cache-Control: no-cache` on "regenerate"? If not, regenerating returns the cached answer for up to an hour.

## Known issues
- Gemini's OpenAI-compat endpoint is beta. Observed: it puts `usage` on every stream chunk (no separate final chunk), and `total_tokens` includes thinking tokens that `completion_tokens` does not (e.g. 5 out, 296 total). We store only input/output, so thinking tokens are not logged yet; matters for Stage 4 cost.
- Stage 4 cost must skip `cache_status = 'hit'` rows: they keep the stored response's token counts (decision 0007).
- The cache stores response text in the DB until it expires, even with `LOG_CONTENT=false`.
- `gemini-2.5-flash` is no longer available to new keys; use `gemini-3.6-flash`.
- Anthropic's OpenAI-compat layer is not production-grade and ignores some fields (decision 0004).
- Requests rejected before the service (invalid JSON body) are not logged.
- Don't replace `SSEResponse`'s `finally` close with a `BackgroundTask`, or the shielded write with plain `to_thread`: both lose rows on client disconnect (decision 0005).
- A first fallback to qwen2.5 can take ~100s while Ollama loads the model (observed). With the 120s timeout per attempt, a hanging provider can take minutes to give up (decision 0006).
- On Windows a refused local connection takes ~2s per attempt (OS-level SYN retries), so an unreachable Ollama costs ~6s before erroring.
