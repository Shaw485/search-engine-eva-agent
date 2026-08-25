"""Pinned source files and Stage 1 configuration contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DataContractError(ValueError):
    """Raised when an ESCI source or derived table breaks its contract."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DatasetLock:
    repository: str
    commit: str
    license: str
    files: tuple[SourceSpec, ...]


@dataclass(frozen=True, slots=True)
class Stage1Config:
    schema_version: str
    source_commit: str
    locale: str
    dataset_version_column: str
    dataset_version_value: int
    official_train_value: str
    official_test_value: str
    dev_query_count: int
    smoke_query_count: int
    split_seed: str
    valid_labels: tuple[str, ...]

    @classmethod
    def from_path(cls, path: str | Path) -> Stage1Config:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        payload["valid_labels"] = tuple(payload["valid_labels"])
        config = cls(**payload)
        if config.dev_query_count < 1:
            raise DataContractError("dev_query_count must be at least 1")
        if not 1 <= config.smoke_query_count <= config.dev_query_count:
            raise DataContractError(
                "smoke_query_count must be between 1 and dev_query_count"
            )
        if set(config.valid_labels) != {"E", "S", "C", "I"}:
            raise DataContractError("valid_labels must contain exactly E, S, C, I")
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_commit": self.source_commit,
            "locale": self.locale,
            "dataset_version_column": self.dataset_version_column,
            "dataset_version_value": self.dataset_version_value,
            "official_train_value": self.official_train_value,
            "official_test_value": self.official_test_value,
            "dev_query_count": self.dev_query_count,
            "smoke_query_count": self.smoke_query_count,
            "split_seed": self.split_seed,
            "valid_labels": list(self.valid_labels),
        }


def load_dataset_lock(path: str | Path) -> DatasetLock:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        files = tuple(SourceSpec(**item) for item in payload["files"])
        lock = DatasetLock(
            repository=payload["repository"],
            commit=payload["commit"],
            license=payload["license"],
            files=files,
        )
    except (KeyError, TypeError) as exc:
        raise DataContractError("dataset lock has an invalid schema") from exc
    if not files:
        raise DataContractError("dataset lock must contain source files")
    for item in files:
        if item.size < 1 or len(item.sha256) != 64:
            raise DataContractError(f"invalid lock entry for {item.path}")
    return lock


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_source_paths(source_dir: str | Path, lock: DatasetLock) -> dict[str, Path]:
    source_root = Path(source_dir)
    return {
        Path(item.path).name: source_root / Path(item.path).name for item in lock.files
    }


def validate_source_files(
    source_dir: str | Path, lock: DatasetLock
) -> dict[str, dict[str, str | int]]:
    """Verify every source before Polars tries to parse it."""

    paths = resolve_source_paths(source_dir, lock)
    evidence: dict[str, dict[str, str | int]] = {}
    for spec in lock.files:
        filename = Path(spec.path).name
        path = paths[filename]
        if not path.is_file():
            raise DataContractError(
                f"missing ESCI source {path}; run bash scripts/download_esci.sh"
            )
        with path.open("rb") as handle:
            prefix = handle.read(128)
        if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise DataContractError(
                f"{path} is a Git LFS pointer, not the real dataset; "
                "run bash scripts/download_esci.sh"
            )
        actual_size = path.stat().st_size
        if actual_size != spec.size:
            raise DataContractError(
                f"size mismatch for {filename}: expected {spec.size}, got {actual_size}"
            )
        actual_sha = sha256_file(path)
        if actual_sha != spec.sha256:
            raise DataContractError(
                f"SHA-256 mismatch for {filename}: expected {spec.sha256}, "
                f"got {actual_sha}"
            )
        if path.suffix == ".parquet":
            with path.open("rb") as handle:
                start_magic = handle.read(4)
                handle.seek(-4, 2)
                end_magic = handle.read(4)
            if start_magic != b"PAR1" or end_magic != b"PAR1":
                raise DataContractError(f"{filename} is not a complete Parquet file")
        evidence[filename] = {"size": actual_size, "sha256": actual_sha}
    return evidence
