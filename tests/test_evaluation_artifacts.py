from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from search_quality.evaluation.artifacts import (
    atomic_write_text,
    write_immutable_json,
    write_immutable_text,
)


def test_mutable_pointer_replacement_is_atomic_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    pointer = tmp_path / "latest.txt"
    atomic_write_text(pointer, "first.json\n")
    atomic_write_text(pointer, "second.json\n")
    assert pointer.read_text(encoding="utf-8") == "second.json\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_immutable_text_is_idempotent_and_rejects_different_contents(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.txt"
    write_immutable_text(artifact, "same\n")
    write_immutable_text(artifact, "same\n")
    with pytest.raises(RuntimeError, match="immutable artifact collision"):
        write_immutable_text(artifact, "different\n")
    assert artifact.read_text(encoding="utf-8") == "same\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_immutable_writers_never_clobber_each_other(tmp_path: Path) -> None:
    artifact = tmp_path / "concurrent.txt"
    barrier = Barrier(2)

    def publish(contents: str) -> str:
        barrier.wait()
        try:
            write_immutable_text(artifact, contents)
        except RuntimeError:
            return "collision"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, ("first\n", "second\n")))

    assert sorted(outcomes) == ["collision", "published"]
    assert artifact.read_text(encoding="utf-8") in {"first\n", "second\n"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_non_finite_json_failure_leaves_no_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "invalid.json"
    with pytest.raises(ValueError, match="Out of range float values"):
        write_immutable_json(artifact, {"metric": float("nan")})
    assert not artifact.exists()


def test_immutable_writer_rejects_an_existing_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same\n", encoding="utf-8")
    artifact = tmp_path / "artifact.txt"
    artifact.symlink_to(target)

    with pytest.raises(RuntimeError, match="symbolic link"):
        write_immutable_text(artifact, "same\n")
    assert target.read_text(encoding="utf-8") == "same\n"
