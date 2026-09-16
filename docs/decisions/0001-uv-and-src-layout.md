# 0001: uv for project management, `src/` layout

- **Status:** accepted
- **Stage:** 0

## Context
Need a Python dependency setup that is reliable on Windows, reproducible across sessions, and simple for a beginner.

## Decision
Use uv (`pyproject.toml` + committed `uv.lock`, `.python-version` = 3.13) with a `src/llm_gateway/` package. Run everything through `uv run ...`. `link-mode = "copy"` because the uv cache (C:) and repo (D:) are on different drives.

## Alternatives
- pip + venv + requirements.txt - no lockfile of the full tree by default; venv activation is awkward in PowerShell (execution policy).
- Poetry - slower, separate Python management, more config.
- Flat layout (package at repo root) - tests can accidentally import local files instead of the installed package, hiding packaging bugs.

## Why
uv is one fast tool for Python versions, venvs, installs and locking. `uv run` always uses the project's environment, so there's no "forgot to activate the venv" class of bugs. The lockfile makes every machine and session identical. The `src/` layout forces tests to run against the package as installed.
