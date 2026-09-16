# Roadmap

Each stage ships on its own branch(es) and must meet its acceptance criteria before the next starts.
Design rule for every stage: later stages must fit without a rewrite (routes thin, one service call to wrap, schema migrations via `PRAGMA user_version`).

## Stage 1 - Pass-through + logging
- `POST /v1/chat/completions` (OpenAI-compatible) forwards to Gemini or Ollama via their OpenAI-compatible endpoints; provider chosen from model name using a config prefix map + default provider.
- Streaming: SSE chunks passed through as they arrive; gateway adds `stream_options.include_usage=true`; usage stored when the provider sends it, NULL otherwise.
- One SQLite row per request: timestamp, request id, app (`X-App-Name`, default `unknown`), provider, model, is_stream, input/output tokens, latency, TTFT (streams), HTTP status, error message.
- Prompt/response text storage off by default (`LOG_CONTENT`).
- API keys never appear in logs, errors, or the DB.
- Config via `.env`; `.env.example` committed. Binds 127.0.0.1 only. `GET /health`.
- Upstream failures return an OpenAI-style error and are logged. No retries/fallback.

**Acceptance:** mocked tests pass for normal, streaming, provider error, and API-key-never-in-DB; ruff clean; README explains run + client setup with an empty Results section; live curl works for both providers (normal + streaming) and rows appear in the DB.

## Stage 2 - Retries + fallback
- Retry transient upstream failures (connect errors, timeouts, 429/5xx) with backoff; automatic fallback Gemini -> Ollama with a configured model mapping.
- Log attempts and which provider finally served the request.

**Acceptance:** tests for retry-then-success, retry-exhausted -> fallback, non-retryable error (4xx) not retried; streaming fallback only before the first byte is sent.

## Stage 3 - Response caching
- Exact-match cache keyed on normalized request (model + messages + params); configurable TTL; cache hits logged. Later: semantic cache via embeddings.

**Acceptance:** identical request served from cache with measured latency drop; cache bypass header works; streaming behaviour defined and tested.

## Stage 4 - Usage dashboard
- Tokens, cost (per-model price table), per-app breakdown, latency and TTFT percentiles, error rates.

**Acceptance:** dashboard reads the log DB only; numbers match direct SQL queries on a fixture DB.

## Stage 5 - Routing, budgets, trimming, replay
- Routing rules (by app, model, size), per-app token budgets, context trimming to fit model limits, prompt replay across models for comparison.

**Acceptance:** each feature has tests and a README section; budgets enforced with a clear OpenAI-style error.
