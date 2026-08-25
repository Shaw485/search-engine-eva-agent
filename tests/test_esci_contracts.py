from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from search_quality.data.contracts import (
    DataContractError,
    DatasetLock,
    SourceSpec,
    validate_source_files,
)


def test_lfs_pointer_fails_with_an_actionable_message(tmp_path: Path) -> None:
    contents = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 123\n"
    source = tmp_path / "shopping_queries_dataset_examples.parquet"
    source.write_bytes(contents)
    lock = DatasetLock(
        repository="https://example.invalid/repo",
        commit="fixture",
        license="Apache-2.0",
        files=(
            SourceSpec(
                path=f"shopping_queries_dataset/{source.name}",
                size=len(contents),
                sha256=hashlib.sha256(contents).hexdigest(),
            ),
        ),
    )

    with pytest.raises(DataContractError, match="Git LFS pointer"):
        validate_source_files(tmp_path, lock)


def test_missing_source_names_the_download_command(tmp_path: Path) -> None:
    lock = DatasetLock(
        repository="https://example.invalid/repo",
        commit="fixture",
        license="Apache-2.0",
        files=(
            SourceSpec(
                path="shopping_queries_dataset/missing.parquet",
                size=1,
                sha256="0" * 64,
            ),
        ),
    )
    with pytest.raises(DataContractError, match="download_esci"):
        validate_source_files(tmp_path, lock)
