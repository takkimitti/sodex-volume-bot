#!/usr/bin/env python3
"""Offline, read-only source gate. Never imports or executes trading modules."""

import ast
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]


def is_sensitive_path(name: str) -> bool:
    """Detect accidentally tracked credential *files* (not a content scanner)."""
    filename = PurePosixPath(name).name.lower()
    if filename in {".env.example", ".env.sample", ".env.template"}:
        return False
    return (
        filename == ".env"
        or filename.startswith(".env.")
        or filename.endswith((".key", ".pem", ".p12", ".pfx"))
    )


def inspect_tracked_paths(root: Path, names: list[str]) -> tuple[int, list[str]]:
    """Check tracked files only; report paths/errors, never file contents."""
    count = 0
    errors = []
    for name in names:
        if is_sensitive_path(name):
            errors.append(f"Tracked credential-like file: {name}")
        if not name.endswith(".py"):
            continue
        path = root / name
        if not path.is_file():
            errors.append(f"Missing tracked Python file: {name}")
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=name)
            count += 1
        except (SyntaxError, UnicodeError, OSError) as exc:
            # Report error category and location, not code or credential contents.
            line = f":{exc.lineno}" if isinstance(exc, SyntaxError) and exc.lineno else ""
            errors.append(f"Python parse failed: {name}{line} ({type(exc).__name__})")
    return count, errors


def main() -> int:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        names = [n.decode("utf-8") for n in result.stdout.split(b"\0") if n]
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        print(f"OFFLINE_GATE=FAIL: cannot enumerate tracked files ({type(exc).__name__})", file=sys.stderr)
        return 2

    count, errors = inspect_tracked_paths(REPO_ROOT, names)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"OFFLINE_GATE=FAIL ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print(f"OFFLINE_GATE=PASS ({count} tracked Python files parsed; no credential-like files tracked)")
    print("Scope: static syntax/filename checks only. No runtime, strategy, or exchange verification.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
