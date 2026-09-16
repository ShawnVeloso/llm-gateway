"""PostToolUse hook: format and lint a Python file right after Claude edits it.

Reads the hook payload (JSON) from stdin. Non-Python files are ignored.
Exit 0 = clean (or auto-fixed). Exit 2 = unfixable lint issues, printed to stderr for Claude.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or not file_path.endswith(".py"):
        return 0

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    target = Path(file_path).resolve()
    if not target.is_file() or not target.is_relative_to(project_dir):
        return 0

    def ruff(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["uv", "run", "--quiet", "ruff", *args, str(target)],  # noqa: S607
            cwd=project_dir,
            capture_output=True,
            text=True,
        )

    ruff("format")
    check = ruff("check", "--fix", "--output-format", "concise")
    if check.returncode != 0:
        print(check.stdout.strip() or check.stderr.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
