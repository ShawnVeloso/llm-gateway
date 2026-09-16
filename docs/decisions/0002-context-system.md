# 0002: Context system for AI coding sessions

- **Status:** accepted
- **Stage:** 0

## Context
Each Claude Code session should start with exactly the context it needs, with no re-pasting and minimal tokens. Checked against Claude Code docs (code.claude.com/docs: memory, skills, sub-agents, hooks, permissions) in Sept 2026.

## Decision
- Always loaded: `CLAUDE.md` (<=100 lines) importing `docs/STATUS.md` (<=30 lines, overwritten each handoff).
- On demand via a read-when table: `docs/ROADMAP.md`, `docs/decisions/`, `README.md`.
- Folder-specific gotchas: `.claude/rules/*.md` with `paths:` frontmatter (created only when a real gotcha exists).
- `/handoff` skill with `disable-model-invocation: true` (it commits and pushes, so only the user triggers it).
- Subagents `test-runner` (haiku, Bash/Read/Grep) and `docs-researcher` (web tools) keep noisy output out of the main context.
- `.claude/settings.json`: Read deny rules for secrets, DBs, logs, venv, caches; Bash/PowerShell deny rules for reading `.env` and history-rewriting git; PostToolUse ruff hook; `autoMemoryEnabled: false`.
- No AGENTS.md: only Claude Code is used in this repo.

## Alternatives
- Nested `CLAUDE.md` in source folders - works (loads when files in that folder are read) but is folder-wide and scatters docs into source; path rules target exact files and live in one place.
- Claude Code auto memory - a machine-local memory outside git; disabled so the repo is the single source of truth.
- Hand-maintained changelog / file manifest - duplicates git history and the file tree, and drifts.

## Why
Small always-on context, everything else pulled by explicit triggers. Facts live in one file each. Known limits: Read deny rules cover Claude's file tools, not shell commands, so shell deny rules and a CLAUDE.md rule are defence in depth, not a guarantee. A Read deny also blocks Claude from writing `.env` - the user creates it from `.env.example`.
