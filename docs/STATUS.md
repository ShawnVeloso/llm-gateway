# Status

## Current stage
Stage 1 - pass-through + logging, on branch `feature/stage-1-passthrough`. Stage 0 merged (PRs #1, #2).

## Done
- uv project (`src/` layout), dependencies, ruff + pytest config.
- Context system: CLAUDE.md, STATUS, ROADMAP, decision records, settings (deny rules, ruff hook), `/handoff` skill, `test-runner` and `docs-researcher` subagents.
- Vendored skills: codebase-design, tdd, diagnosing-bugs, grilling/grill-me, resolving-merge-conflicts, frontend-design (see decision 0003).
- Stage 1: `config.py` (Settings via pydantic-settings, fixed 127.0.0.1 bind) + `.env.example` + tests.

## Next step
Stage 1: model -> provider routing (longest prefix match), then provider clients (httpx) with respx-mocked tests.

## Open questions
- Does Lithe call `GET /v1/models`? (Not in Stage 1 unless needed.)

## Known issues
- Gemini's OpenAI-compatible endpoint is beta; streaming usage chunk and error body shape are not fully documented. Confirm with live curl at end of Stage 1.
