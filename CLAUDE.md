# llm-gateway

A local server on the user's Windows PC that sits between their apps (Lithe desktop app, later n8n and scripts) and AI providers. Apps call an OpenAI-compatible `POST /v1/chat/completions` on the gateway; it picks Gemini or Ollama from the model name, forwards the request (including SSE streaming), returns the response, and logs one SQLite row per request. Built in stages (see `docs/ROADMAP.md`). The owner is a student learning AI engineering and git: explain decisions in plain language with the "why"; don't narrate routine steps.

@docs/STATUS.md

## Map (folders)
- `src/llm_gateway/` - the application package (FastAPI app, config, providers, streaming, SQLite logging)
- `tests/` - pytest suite; providers are always mocked with respx, never called for real
- `docs/` - STATUS (current state), ROADMAP (stages + acceptance criteria), `decisions/` (one record per decision)
- `.claude/` - settings (permissions, hooks), `skills/`, `agents/`, `rules/` (path-scoped gotchas), `hooks/`

## Commands (Windows; run from repo root)
- Install/sync: `uv sync`
- Run: `uv run llm-gateway` (binds 127.0.0.1 only)
- Test: `uv run pytest -x -q --tb=short` (or the `test-runner` subagent for full/noisy runs)
- Lint: `uv run ruff check .` / Format check: `uv run ruff format --check .`
- Add a dependency: `uv add <pkg>` (dev: `uv add --dev <pkg>`). Never use pip directly.

## Conventions
- Python 3.13, async everywhere on the request path (httpx.AsyncClient, no blocking I/O in handlers; SQLite writes via `asyncio.to_thread`).
- Routes stay thin; request logic lives in the service layer so later stages (retry, fallback, cache) wrap one call.
- Errors returned to clients are OpenAI-style: `{"error": {"message", "type", "code"}}`.
- Secrets: API keys only in `SecretStr` and outgoing auth headers. Never log headers; pass error text through redaction. Never print, cat, or read `.env`.
- Token counts are only what the provider reports; store NULL otherwise, never estimate.
- Style is enforced by a PostToolUse ruff hook; don't spend turns on formatting.
- New settings go in `config.py` AND `.env.example` in the same commit.

## Git
- Never commit to `main`. Branch from up-to-date main: `feature/...`, `fix/...`, `chore/...`, `docs/...`. One feature per branch.
- Commit messages: `<type>: <description>` with type in feat, fix, docs, test, refactor, chore. Git log is the changelog.
- No AI attribution anywhere: no `Co-Authored-By` trailers, no "Generated with Claude" lines in commits or PRs.
- Push, print the PR link, stop. Don't open or merge PRs.
- Anything forceful or history-rewriting (force push, reset --hard, rebase, amend of pushed commits): stop and ask.

## Context rules
- Follow the read-when table; don't open docs "to be safe". Search for symbols instead of reading whole files.
- Run tests with short output; summarize, never paste long logs.
- "Tested" means tests ran and passed, or state exactly what was observed.
- If this file or STATUS.md becomes wrong, fix it in the same commit as the change that made it wrong.
- Same correction from the user twice -> add a one-line rule here. Near 100 lines -> condense or move detail out.
- Tell the user when the conversation is heavy and it's a good time to `/handoff` and start fresh.
- External docs lookups go through the `docs-researcher` subagent.

## Read when
| Trigger | Read |
|---|---|
| Picking, scoping, or checking done-ness of work | `docs/ROADMAP.md` |
| "Why is it built this way?" / before changing a past decision | `ls docs/decisions/`, then the one matching file |
| Making a new decision worth recording | `docs/decisions/0000-template.md` |
| Human-facing usage (run, client setup) | `README.md` |
| Ending a session | run `/handoff` |
