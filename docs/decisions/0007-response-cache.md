# 0007: Exact-match response cache in SQLite

- **Status:** accepted
- **Stage:** 3

## Context
Scripts and n8n workflows often send the same request again. Each repeat costs tokens and seconds.
Stage 3 needs a cache with a TTL, a way for clients to skip it, defined streaming behaviour, and
cache hits visible in the log.

## Decision
- `CachedChat` (`cache.py`) wraps `ChatService`, and `LoggedChat` wraps `CachedChat`, so hits
  are logged like any other request.
- Key: SHA-256 of `[is_stream, body]` as JSON with sorted keys and no whitespace, with `stream` and
  `stream_options` removed. Every other field counts, including `temperature` and `user`.
- Storage: a `cache` table in the gateway DB (migration 3). Rows are ignored after `expires_at`
  and deleted by the next write. Default TTL 1 hour (`CACHE_TTL_SECONDS`).
- On by default for all requests (`CACHE_ENABLED`). Per request, standard `Cache-Control`:
  `no-cache` skips the lookup but stores the fresh answer; `no-store` does neither.
- Only stored: HTTP 200 answers served by the requested model, not a fallback. A stream is stored
  as its raw SSE bytes only if it ended cleanly; a hit replays them through `ChatStream`, so
  logging reads usage and text the same way as a live stream.
- Log: `requests.cache_status` = `hit` / `miss` / `bypass` (NULL while caching is off). A hit has
  `attempt_count` 0 and the token counts from the stored response. The response carries an
  `X-Cache` header.

## Alternatives
- In-memory dict: simpler and nothing on disk, but empty after every restart, and a later
  semantic cache would need storage anyway.
- Cache only `temperature: 0`: safe for "regenerate", but most clients leave temperature unset,
  so little would be cached.
- Off by default: safest, but the cache would do nothing until configured.
- Never cache streams: simpler, but Lithe streams, so it would never benefit.
- Cache fallback answers: the fallback's answer would keep being served for the whole TTL after
  Gemini recovers.

## Why
Exact match can never return the answer to a different question; any doubt is a miss. SQLite was
already there with versioned migrations, so persistence cost one table. Using standard
`Cache-Control` means clients need no gateway-specific header.

Trade-offs accepted:
- Response text is written to disk until it expires, even with `LOG_CONTENT=false`.
- An identical request gets an identical answer, so a "regenerate" button needs
  `Cache-Control: no-cache`.
- Two identical requests at the same time both miss (no request coalescing).
- Hits keep their token counts, so Stage 4 must leave `cache_status = 'hit'` rows out of cost.
