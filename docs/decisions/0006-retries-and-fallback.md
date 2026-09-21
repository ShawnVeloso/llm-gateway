# 0006: Retries, fallback, and an attempts table

- **Status:** accepted
- **Stage:** 2

## Context
Gemini's free tier returns 429s and occasional 5xx/timeouts, and Ollama may not be running. Apps should get an answer when a failure is temporary, but a retry must never hide a real mistake (bad model name, missing key), and a stream the client has already started reading can't switch provider.

## Decision
- Retry only connect errors, timeouts, 429 and 5xx (`retry.py`). Other 4xx are returned at once.
- Backoff: `RETRY_BASE_DELAY_SECONDS` doubling, with jitter, capped at `RETRY_MAX_DELAY_SECONDS`; `MAX_RETRIES` per provider. A `Retry-After` within the cap is obeyed; a longer one ends retries on that provider immediately.
- After retries run out, `FALLBACK_MODELS` (provider -> model, default `{"gemini": "ollama/qwen2.5"}`) gives one fallback route. No chaining. A fallback whose provider has no key is skipped with a warning.
- `provider_not_configured` never falls back.
- Streams: `ChatStream.start()` reads the first chunk before anything is sent to the client, so failures up to the first byte are retried/fall back like non-streaming ones. After that, the Stage 1 in-band error applies.
- The loop lives in `ChatService._run`, shared by `complete` and `stream`; results carry every `Attempt`.
- Logging: new `attempts` table (one row per upstream call, linked by `request_id`), plus `requested_model` and `attempt_count` on `requests`; `provider`/`model` mean who finally served it. Schema changes are numbered `MIGRATIONS` applied at startup using `PRAGMA user_version`.

## Alternatives
- Summary columns only (`attempt_count`, `fell_back`) - loses which tries failed and why, which Stage 4 error rates need.
- Per-model fallback map - more precise, but every new Gemini model name would need an entry.
- Falling back on any error - would hide config mistakes behind a weaker local model.
- A retry library (tenacity) - the policy is ~20 lines, and our loop must also handle fallback and streams.

## Why
Retrying only failures that can go away keeps errors honest. Reading the first chunk costs nothing (the client can't get bytes sooner anyway) and makes "fall back before the first byte" literally true. One row per attempt lets the dashboard compute per-provider failure rates without guessing. Trade-off: with the default 120s timeout, a hanging provider can take several minutes to give up (3 tries plus fallback); lower `REQUEST_TIMEOUT_SECONDS` or `MAX_RETRIES` if that hurts.
