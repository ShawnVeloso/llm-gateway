---
name: handoff
description: End-of-session handoff - update STATUS.md, record decisions, verify CLAUDE.md, commit, push, print PR link.
disable-model-invocation: true
---

Hand off this session so the next one starts clean. Do these in order and stop on any failure.

1. **Branch check.** Run `git branch --show-current`. If it is `main`, stop and ask the user which branch to use. Never commit to main.
2. **Verify.** Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest -x -q --tb=short`. Report results as counts only. If anything fails, stop and tell the user; do not hand off a broken branch unless they say so, and then record it under "Known issues".
3. **Overwrite `docs/STATUS.md`** (hard cap 30 lines, no history, no dates-as-log). Sections: Current stage, Done (this stage only), Next step (one exact, actionable step), Open questions, Known issues. Write "None" for empty sections.
4. **Decision records.** For each meaningful decision made this session that has no record yet, add `docs/decisions/NNNN-short-kebab-title.md` (next number from `ls docs/decisions`) using the template in `docs/decisions/0000-template.md`. Keep each under ~25 lines.
5. **Check `CLAUDE.md`** against reality: commands, folder map, conventions, read-when table. Fix anything wrong. Keep it under 100 lines; move detail to on-demand docs rather than growing it.
6. **Commit** remaining changes with `<type>: <description>` (usually `docs: update status for handoff`). No AI attribution lines or trailers.
7. **Push** with `git push -u origin <branch>`.
8. **PR link.** Print `https://github.com/ShawnVeloso/llm-gateway/compare/main...<branch>?expand=1`, or the existing PR URL from `gh pr view --json url -q .url` if one exists. Do not open or merge PRs.
9. Tell the user in one or two lines: what is on the branch, the link, and that it is safe to clear the session (`/clear`).
