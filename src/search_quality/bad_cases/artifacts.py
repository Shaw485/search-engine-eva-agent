"""Confined storage and cross-process locking for Bad Case diagnostics."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from search_quality.evaluation.artifacts import atomic_write_text, write_immutable_json

from .contracts import (
    BadCaseDiagnosticArtifact,
    BadCaseExecutionReceipt,
    BadCaseFailedAttempt,
)

logger = logging.getLogger("search_quality.bad_case")
MAX_BAD_CASE_STORE_BYTES = 256 * 1024 * 1024
MIN_FREE_SPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024


class BadCaseRunInProgress(RuntimeError):
    """Another process already owns the diagnostics run lock."""


@contextmanager
def bad_case_run_lock(artifact_root: str | Path) -> Iterator[Path]:
    """Acquire one non-blocking lock shared by CLI and API processes."""

    base = trusted_bad_case_root(artifact_root)
    lock_path = base / ".run.lock"
    if lock_path.is_symlink():
        raise ValueError("Bad Case run lock must not be a symbolic link")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Bad Case run lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BadCaseRunInProgress(
                "Bad Case diagnostics are already running"
            ) from exc
        yield base
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_bad_case_capacity(base: Path) -> None:
    observed = sum(
        path.stat().st_size
        for path in base.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if observed >= MAX_BAD_CASE_STORE_BYTES:
        raise RuntimeError("Bad Case artifact store exceeds its size limit")
    if shutil.disk_usage(base).free < MIN_FREE_SPACE_BYTES:
        raise RuntimeError("Bad Case artifact store has insufficient free space")


def store_bad_case_artifacts(
    *,
    artifact_root: str | Path,
    artifact: BadCaseDiagnosticArtifact,
    execution: BadCaseExecutionReceipt,
) -> tuple[Path, Path]:
    base = trusted_bad_case_root(artifact_root)
    latest = base / "latest.txt"
    if latest.is_symlink():
        raise ValueError("Bad Case latest pointer must not be a symbolic link")
    evidence_dir = _trusted_child(base, "evidence")
    execution_dir = _trusted_child(base, "executions")
    evidence_path = evidence_dir / f"{artifact.diagnostic_id}.json"
    execution_path = execution_dir / f"{execution.execution_id}.json"
    _require_payload_size(
        artifact.model_dump(mode="json"),
        maximum=MAX_EVIDENCE_BYTES,
        label="Bad Case evidence",
    )
    _require_payload_size(
        execution.model_dump(mode="json"),
        maximum=MAX_RECEIPT_BYTES,
        label="Bad Case execution receipt",
    )
    # A completed execution is published only by its receipt. Write the
    # content-addressed evidence first so a second-write failure can leave at
    # most an unreferenced deterministic artifact, never a completed receipt.
    write_immutable_json(evidence_path, artifact.model_dump(mode="json"))
    write_immutable_json(execution_path, execution.model_dump(mode="json"))
    try:
        atomic_write_text(latest, f"{artifact.diagnostic_id}\n")
    except OSError as exc:
        # The pointer is only a convenience. Both immutable artifacts already
        # exist and form the source of truth.
        logger.warning(
            "bad_case_latest_pointer_failed",
            extra={"error_type": type(exc).__name__},
        )
    logger.info(
        "bad_case_artifacts_stored",
        extra={
            "diagnostic_candidate_count": artifact.diagnostic_candidate_count,
            "diagnostic_id": artifact.diagnostic_id,
            "execution_id": execution.execution_id,
            "query_count": artifact.query_count,
        },
    )
    return evidence_path, execution_path


def store_failed_attempt(
    *,
    artifact_root: str | Path,
    attempt: BadCaseFailedAttempt,
) -> Path:
    base = trusted_bad_case_root(artifact_root)
    attempt_dir = _trusted_child(base, "attempts")
    path = attempt_dir / f"{attempt.execution_id}.json"
    _require_payload_size(
        attempt.model_dump(mode="json"),
        maximum=MAX_RECEIPT_BYTES,
        label="Bad Case failed attempt",
    )
    write_immutable_json(path, attempt.model_dump(mode="json"))
    logger.info(
        "bad_case_failed_attempt_stored",
        extra={
            "completed_query_count": attempt.completed_query_count,
            "error_code": attempt.error_code,
            "execution_id": attempt.execution_id,
        },
    )
    return path


def trusted_bad_case_root(artifact_root: str | Path) -> Path:
    configured = Path(artifact_root)
    if not configured.is_absolute():
        raise ValueError("Bad Case artifact root must be absolute")
    _reject_existing_symlink_components(configured)
    if configured.is_symlink():
        raise ValueError("Bad Case artifact root must not be a symbolic link")
    configured.mkdir(parents=True, exist_ok=True)
    root = configured.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Bad Case artifact root must be a directory")
    return _trusted_child(root, "bad-case-diagnostics")


def _trusted_child(parent: Path, name: str) -> Path:
    child = parent / name
    if child.is_symlink():
        raise ValueError("Bad Case artifact directory must not be a symbolic link")
    child.mkdir(parents=True, exist_ok=True)
    resolved = child.resolve(strict=True)
    if resolved.parent != parent:
        raise ValueError("Bad Case artifact directory escaped its configured root")
    return resolved


def _reject_existing_symlink_components(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink():
                raise ValueError(
                    "Bad Case artifact path must not contain a symbolic link"
                )


def _require_payload_size(
    payload: dict[str, object],
    *,
    maximum: int,
    label: str,
) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise RuntimeError(f"{label} exceeds its size limit")
