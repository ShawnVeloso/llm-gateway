# Status

## Current stage
Stage 1 - pass-through + logging, on branch `feature/stage-1-passthrough`. Stage 0 merged (PRs #1, #2).

## Done
- uv project (`src/` layout), dependencies, ruff + pytest config.
- Context system: CLAUDE.md, STATUS, ROADMAP, decision records, settings (deny rules, ruff hook), `/handoff` skill, `test-runner` and `docs-researcher` subagents.
- Vendored skills: codebase-design, tdd, diagnosing-bugs, grilling/grill-me, resolving-merge-conflicts, frontend-design (see decision 0003).
- Stage 1: `config.py` (Settings via pydantic-settings, fixed 127.0.0.1 bind) + `.env.example` + tests.
- Stage 1: `routing.py` - model -> provider (Gemini/Ollama/OpenAI/Anthropic) via `provider/model` or longest prefix; missing key raises `ProviderNotConfiguredError` (decision 0004).

- Stage 1: `providers.py` (one httpx POST for all providers) + `service.py` `ChatService.complete` (non-streaming): routes, strips `stream` flags, maps not-configured (400), upstream HTTP errors (status kept, key redacted), connect errors (502), timeouts (504) to OpenAI-style errors. Returns `ChatResult`, never raises.

## Next step
Stage 1: FastAPI app - `POST /v1/chat/completions` (non-streaming via `ChatService`), `GET /health`, shared `httpx.AsyncClient` in lifespan, wire `uv run llm-gateway` to uvicorn on 127.0.0.1. Then streaming, then SQLite logging.

## Open questions
- Does Lithe call `GET /v1/models`? (Not in Stage 1 unless needed.)

## Known issues
- Gemini's OpenAI-compatible endpoint is beta; streaming usage chunk and error body shape are not fully documented. Confirm with live curl at end of Stage 1.
- Anthropic's OpenAI-compat layer is not production-grade and ignores some fields (see decision 0004).
