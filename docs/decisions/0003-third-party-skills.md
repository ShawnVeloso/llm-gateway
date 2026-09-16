# 0003: Vendored third-party skills

- **Status:** accepted
- **Stage:** 0

## Context
The user wanted proven community skills in the workflow. Every skill's one-line description loads into every session, so each one must earn its place, and third-party instructions must not override repo rules.

## Decision
Vendor these verbatim (read in full before committing) into `.claude/skills/`:

| Skill | Source (pinned commit) | Use |
|---|---|---|
| codebase-design | mattpocock/skills `skills/engineering/codebase-design` @ 959a8e9 | module/interface/seam design vocabulary |
| tdd | mattpocock/skills `skills/engineering/tdd` @ 959a8e9 | test-first loop, good tests, when to mock |
| diagnosing-bugs | mattpocock/skills `skills/engineering/diagnosing-bugs` @ 959a8e9 | build a feedback loop before guessing |
| grilling, grill-me | mattpocock/skills `skills/productivity/*` @ 959a8e9 | stress-test a stage plan before building (`/grill-me`) |
| resolving-merge-conflicts | mattpocock/skills `skills/engineering/*` @ 959a8e9 | conflict resolution procedure |
| frontend-design | anthropics/skills `skills/frontend-design` @ 34040c9 | Stage 4 dashboard UI |

`find-skills` (vercel-labs/skills) stays installed at user level (`~/.claude/skills`), since it is not project-specific; it matched upstream when checked. MIT notice: `docs/licenses/mattpocock-skills-MIT.txt`; Apache-2.0 notice ships inside `frontend-design/`.

Repo rules in `CLAUDE.md` win over any skill. To update: re-download the same paths at a new commit, read the diff, update this table.

## Alternatives
- `npx skills add` - adds a CLI and lock/symlink layout (symlinks are fragile on Windows) for a handful of markdown files.
- Matt Pocock's `handoff`, `git-guardrails-claude-code`, `teach` - duplicate our `/handoff` or deny rules, or write learning files into the repo.
- `webapp-testing` - defer to Stage 4, when there is a web UI to test.

## Why
Verbatim copies pinned to a commit are auditable and easy to update. Not editing vendor files keeps updates a clean diff; the precedence rule handles conflicts (e.g. the merge-conflict skill says "never abort" and "continue the rebase", but rebase/abort decisions stay with the user).
