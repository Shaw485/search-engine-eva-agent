"""Confined loading for diagnostic-planning inputs selected only by safe IDs."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

from search_quality.bad_cases.contracts import BadCaseDiagnosticArtifact
from search_quality.query_constructor.contracts import QuerySetArtifact

from .contracts import (
    DIAGNOSTIC_ID_PATTERN,
    QUERY_SET_ID_PATTERN,
    ResolvedDiagnosticEvidence,
)
from .resolver import resolve_diagnostic_evidence

logger = logging.getLogger("search_quality.diagnostic_experiments")

MAX_DIAGNOSTIC_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_QUERY_SET_ARTIFACT_BYTES = 4 * 1024 * 1024


def load_resolved_diagnostic_evidence(
    *,
    artifact_root: str | Path,
    diagnostic_id: str,
    query_set_id: str,
) -> ResolvedDiagnosticEvidence:
    """Load one fixed diagnostic/Query-set pair without accepting file paths."""

    try:
        artifact, query_set = load_diagnostic_artifacts(
            artifact_root=artifact_root,
            diagnostic_id=diagnostic_id,
            query_set_id=query_set_id,
        )
        evidence = resolve_diagnostic_evidence(
            artifact=artifact,
            query_set=query_set,
        )
    except Exception as exc:
        logger.warning(
            "diagnostic_experiment_artifact_load_failed",
            extra={"error_type": type(exc).__name__},
        )
        raise
    logger.info(
        "diagnostic_experiment_artifacts_loaded",
        extra={
            "diagnostic_id": evidence.diagnostic_id,
            "query_set_id": evidence.query_set_id,
        },
    )
    return evidence


def load_diagnostic_artifacts(
    *,
    artifact_root: str | Path,
    diagnostic_id: str,
    query_set_id: str,
) -> tuple[BadCaseDiagnosticArtifact, QuerySetArtifact]:
    """Load and cross-validate one immutable pair selected only by fixed IDs.

    This is the raw-artifact counterpart to ``load_resolved_diagnostic_evidence``
    for trusted server workflows such as the Human Diagnostic Oracle.  Callers
    cannot provide a filename or directory, and both artifacts are validated
    against their content-derived IDs before they are returned.
    """

    safe_diagnostic_id = _strict_id(
        diagnostic_id,
        pattern=DIAGNOSTIC_ID_PATTERN,
        label="diagnostic_id",
    )
    safe_query_set_id = _strict_id(
        query_set_id,
        pattern=QUERY_SET_ID_PATTERN,
        label="query_set_id",
    )
    root = _trusted_existing_root(artifact_root)
    evidence_dir = _trusted_existing_child(
        _trusted_existing_child(root, "bad-case-diagnostics"),
        "evidence",
    )
    query_set_dir = _trusted_existing_child(root, "query-sets")
    diagnostic_bytes = _read_regular_file(
        evidence_dir / f"{safe_diagnostic_id}.json",
        maximum=MAX_DIAGNOSTIC_ARTIFACT_BYTES,
        label="diagnostic artifact",
    )
    query_set_bytes = _read_regular_file(
        query_set_dir / f"{safe_query_set_id}.json",
        maximum=MAX_QUERY_SET_ARTIFACT_BYTES,
        label="Query-set artifact",
    )
    artifact = _validate_json_artifact(
        diagnostic_bytes,
        model=BadCaseDiagnosticArtifact,
        label="diagnostic artifact",
    )
    query_set = _validate_json_artifact(
        query_set_bytes,
        model=QuerySetArtifact,
        label="Query-set artifact",
    )
    if artifact.diagnostic_id != safe_diagnostic_id:
        raise ValueError("diagnostic artifact ID does not match its filename")
    if query_set.query_set_id != safe_query_set_id:
        raise ValueError("Query-set artifact ID does not match its filename")
    # Re-run all cross-artifact/content-ID checks before exposing raw content.
    resolve_diagnostic_evidence(artifact=artifact, query_set=query_set)
    logger.info(
        "diagnostic_experiment_raw_artifacts_loaded",
        extra={
            "diagnostic_id": artifact.diagnostic_id,
            "query_set_id": query_set.query_set_id,
        },
    )
    return artifact, query_set


def _strict_id(value: str, *, pattern: str, label: str) -> str:
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise ValueError(f"{label} has an invalid format")
    return value


def _trusted_existing_root(artifact_root: str | Path) -> Path:
    configured = Path(artifact_root)
    if not configured.is_absolute():
        raise ValueError("diagnostic artifact root must be absolute")
    _reject_existing_symlink_components(configured)
    resolved = configured.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("diagnostic artifact root must be a directory")
    return resolved


def _trusted_existing_child(parent: Path, name: str) -> Path:
    child = parent / name
    if child.is_symlink():
        raise ValueError("diagnostic artifact directory must not be a symbolic link")
    resolved = child.resolve(strict=True)
    if resolved.parent != parent or not resolved.is_dir():
        raise ValueError("diagnostic artifact directory escaped its fixed root")
    return resolved


def _reject_existing_symlink_components(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if (cursor.exists() or cursor.is_symlink()) and cursor.is_symlink():
            raise ValueError("diagnostic artifact path contains a symbolic link")


def _read_regular_file(path: Path, *, maximum: int, label: str) -> bytes:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if metadata.st_size > maximum:
            raise ValueError(f"{label} exceeds its size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            encoded = handle.read(maximum + 1)
        if len(encoded) > maximum:
            raise ValueError(f"{label} exceeds its size limit")
        return encoded
    finally:
        os.close(descriptor)


def _validate_json_artifact(
    encoded: bytes,
    *,
    model: type[BadCaseDiagnosticArtifact] | type[QuerySetArtifact],
    label: str,
) -> BadCaseDiagnosticArtifact | QuerySetArtifact:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError(f"{label} contains a non-finite number")

    payload = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    normalized = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return model.model_validate_json(normalized, strict=True)
