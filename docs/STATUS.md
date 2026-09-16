# Status

## Current stage
Stage 0 - context system and project skeleton. Skeleton + context system merged (PR #1); vendored skills + .gitattributes on branch `chore/vendor-skills`.

## Done
- uv project (`src/` layout), dependencies, ruff + pytest config.
- Context system: CLAUDE.md, STATUS, ROADMAP, decision records, settings (deny rules, ruff hook), `/handoff` skill, `test-runner` and `docs-researcher` subagents.
- Vendored skills: codebase-design, tdd, diagnosing-bugs, grilling/grill-me, resolving-merge-conflicts, frontend-design (see decision 0003).

## Next step
User merges the `chore/vendor-skills` PR. Then: `git switch main; git pull`, create `feature/stage-1-passthrough`, and implement Stage 1 starting with `config.py` + `.env.example`.

## Open questions
- Does Lithe call `GET /v1/models`? (Not in Stage 1 unless needed.)

## Known issues
- Gemini's OpenAI-compatible endpoint is beta; streaming usage chunk and error body shape are not fully documented. Confirm with live curl at end of Stage 1.
