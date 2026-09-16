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
- Stage 1: `app.py` `create_app` - `GET /health`, `POST /v1/chat/completions` (non-streaming; `stream: true` gets a temporary 400), one shared `httpx.AsyncClient` per app via lifespan; `uv run llm-gateway` serves it with uvicorn on 127.0.0.1.

## Next step
Stage 1: streaming - SSE pass-through in the service, add `stream_options.include_usage=true`, remove the temporary `streaming_not_supported` 400 in `app.py`. Then SQLite logging.

## Open questions
- Does Lithe call `GET /v1/models`? (Not in Stage 1 unless needed.)

## Known issues
- Gemini's OpenAI-compatible endpoint is beta; streaming usage chunk and error body shape are not fully documented. Confirm with live curl at end of Stage 1.
- Anthropic's OpenAI-compat layer is not production-grade and ignores some fields (see decision 0004).
