"""Approved smoke/dev dataset profiles for routine Stage 2 evaluation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_PROFILES = frozenset({"smoke", "dev"})


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trusted_project_file(
    *,
    project_root: str | Path,
    relative_path: str | Path,
    max_bytes: int | None = None,
) -> Path:
    """Return one fixed project file after rejecting path indirection.

    Callers supply a code-owned relative path, never user input.  Every path
    component is checked before a later parser or hash function can open it.
    """

    root = Path(project_root).resolve(strict=True)
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("trusted project file must use a contained relative path")
    path = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(
                "trusted project file path must not contain a symbolic link"
            )
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.resolve(strict=True) != path:
        raise ValueError("trusted project file escaped its fixed location")
    if max_bytes is not None:
        if max_bytes < 1:
            raise ValueError("trusted project file size limit must be positive")
        if path.stat().st_size > max_bytes:
            raise ValueError("trusted project file exceeds its size limit")
    return path


@dataclass(frozen=True, slots=True)
class EvaluationProfile:
    """Immutable contract tying one run to a Stage 1 output and manifest."""

    profile_id: str
    path: Path
    file_sha256: str
    canonical_sha256: str
    stage1_manifest_sha256: str
    stage1_schema_version: str
    source_commit: str
    expected_rows: int
    expected_queries: int
    expected_products: int

    def __post_init__(self) -> None:
        if self.profile_id not in ALLOWED_PROFILES:
            raise ValueError(f"unsupported routine profile {self.profile_id!r}")
        for name in (
            "file_sha256",
            "canonical_sha256",
            "stage1_manifest_sha256",
            "stage1_schema_version",
            "source_commit",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("expected_rows", "expected_queries", "expected_products"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        object.__setattr__(self, "path", Path(self.path))

    @classmethod
    def from_stage1_manifest(
        cls,
        *,
        profile_id: str,
        project_root: str | Path,
        manifest_path: str | Path,
    ) -> EvaluationProfile:
        if profile_id not in ALLOWED_PROFILES:
            raise ValueError(f"unsupported routine profile {profile_id!r}")
        root = Path(project_root)
        manifest_file = Path(manifest_path)
        payload: dict[str, Any] = json.loads(manifest_file.read_text(encoding="utf-8"))
        output = payload["outputs"][profile_id]
        profile = payload["profiles"][profile_id]
        if profile_id == "smoke":
            path = root / "data" / "samples" / "esci-stage1-smoke.parquet"
        else:
            path = root / "data" / "processed" / "esci-stage1-v1" / output["path"]
        return cls(
            profile_id=profile_id,
            path=path,
            file_sha256=output["file_sha256"],
            canonical_sha256=output["canonical_sha256"],
            stage1_manifest_sha256=sha256_file(manifest_file),
            stage1_schema_version=payload["schema_version"],
            source_commit=payload["source"]["commit"],
            expected_rows=profile["rows"],
            expected_queries=profile["queries"],
            expected_products=profile["products"],
        )

    def to_manifest_dict(self) -> dict[str, str | int]:
        return {
            "canonical_sha256": self.canonical_sha256,
            "file": self.path.name,
            "file_sha256": self.file_sha256,
            "profile": self.profile_id,
            "source_commit": self.source_commit,
            "stage1_manifest_sha256": self.stage1_manifest_sha256,
            "stage1_schema_version": self.stage1_schema_version,
        }


def trusted_profile_data_path(
    profile: EvaluationProfile,
    *,
    project_root: str | Path,
) -> Path:
    """Resolve an allowlisted profile path without following any symlink."""

    root = Path(project_root).resolve(strict=True)
    requested = Path(profile.path)
    if not requested.is_absolute():
        requested = root / requested
    if profile.profile_id == "smoke":
        trusted_parent = root / "data" / "samples"
        expected = trusted_parent / "esci-stage1-smoke.parquet"
        if requested != expected:
            raise ValueError("smoke profile path is outside the trusted contract")
    else:
        trusted_parent = root / "data" / "processed" / "esci-stage1-v1"
        expected = None
        try:
            requested.relative_to(trusted_parent)
        except ValueError as exc:
            raise ValueError(
                "evaluation profile path escaped its trusted root"
            ) from exc

    try:
        relative = requested.relative_to(root)
    except ValueError as exc:
        raise ValueError("evaluation profile path escaped the project root") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("evaluation profile path must not contain a symbolic link")
    if not requested.is_file():
        raise FileNotFoundError(requested)
    resolved = requested.resolve(strict=True)
    if expected is not None and resolved != expected:
        raise ValueError("smoke profile path does not match its fixed location")
    try:
        resolved.relative_to(trusted_parent)
    except ValueError as exc:
        raise ValueError("evaluation profile path escaped its trusted root") from exc
    return resolved
