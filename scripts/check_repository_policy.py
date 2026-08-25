"""Fail CI when repository contents violate the Stage 0 safety boundary."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 10 * 1024 * 1024
BLOCKED_DIRECTORIES = ("data/raw/", "data/processed/", "runs/")
BLOCKED_FILENAMES = {".env", "id_rsa", "id_ed25519"}
PRIVATE_KEY_PATTERN = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
SECRET_PATTERNS = {
    "GitHub token": re.compile(
        rb"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,})"
    ),
    "OpenAI-style key": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
}


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        PROJECT_ROOT / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    ]


def find_violations(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith(BLOCKED_DIRECTORIES):
            violations.append(f"generated/raw data must not be committed: {relative}")
        if path.name in BLOCKED_FILENAMES:
            violations.append(f"local secret file must not be committed: {relative}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            violations.append(
                f"file exceeds the 10 MiB repository limit: {relative} ({size} bytes)"
            )
        if size <= MAX_FILE_BYTES:
            contents = path.read_bytes()
            if PRIVATE_KEY_PATTERN.search(contents):
                violations.append(f"private key material detected: {relative}")
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(contents):
                    violations.append(f"{label} detected: {relative}")
    return violations


def main() -> None:
    violations = find_violations(repository_files())
    if violations:
        raise SystemExit("repository policy failed:\n- " + "\n- ".join(violations))
    print("repository policy passed")


if __name__ == "__main__":
    main()
