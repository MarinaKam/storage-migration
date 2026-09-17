"""Check public file scope and generic, English-only working-tree content."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    ".gitignore",
    ".python-version",
    "Makefile",
    "pyproject.toml",
    "README.md",
    ".github/workflows/ci.yml",
}


def main():
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    failures = []
    for name in sorted(set(filter(None, names))):
        path = ROOT / name
        if not path.exists():
            continue  # Deleted tracked files will disappear in the next commit.
        if name not in ALLOWED and not re.fullmatch(r"scripts/[A-Za-z0-9_]+\.py", name):
            failures.append((name, "outside public file list"))
            continue
        if path.is_symlink():
            failures.append((name, "symlink"))
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"[\u0400-\u04ff]", text):
            failures.append((name, "non-English Cyrillic content"))
        if re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+", text):
            failures.append((name, "personal absolute path"))
        if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
            failures.append((name, "private-key block"))
    for name, reason in failures:
        print(f"{name}: {reason}")
    if not failures:
        print("Public file scope and generic-content checks passed (not a complete secret scan).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
