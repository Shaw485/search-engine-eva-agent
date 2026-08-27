"""Durable helpers shared by formal evaluation artifact CLIs."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def require_clean_code_revision(project_root: Path) -> str:
    """Return HEAD only when every tracked/untracked project change is absent."""

    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    revision = completed.stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(
            "formal evaluation artifacts require a clean Git worktree; commit or "
            "stash changes before running"
        )
    return revision


def atomic_write_text(path: Path, contents: str) -> None:
    """Replace one text artifact atomically after flushing it to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    """Write canonical pretty JSON, rejecting an existing different payload."""

    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    write_immutable_text(path, serialized)


def write_immutable_text(path: Path, contents: str) -> None:
    """Publish once without clobbering a concurrent writer; identical is idempotent."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.is_symlink():
                raise RuntimeError(
                    f"immutable artifact path is a symbolic link at {path}"
                ) from None
            if path.read_text(encoding="utf-8") != contents:
                raise RuntimeError(f"immutable artifact collision at {path}") from None
        else:
            _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
