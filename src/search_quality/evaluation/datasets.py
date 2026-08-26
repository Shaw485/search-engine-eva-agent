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
