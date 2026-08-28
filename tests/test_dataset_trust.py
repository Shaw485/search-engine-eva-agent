from __future__ import annotations

from pathlib import Path

import pytest

from search_quality.evaluation.datasets import trusted_project_file


def test_trusted_project_file_rejects_intermediate_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "esci-stage1.json").write_text("{}", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "manifests").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        trusted_project_file(
            project_root=tmp_path,
            relative_path="data/manifests/esci-stage1.json",
        )


def test_trusted_project_file_enforces_size_before_parser_reads(
    tmp_path: Path,
) -> None:
    config = tmp_path / "configs" / "evaluation"
    config.mkdir(parents=True)
    (config / "policy.json").write_bytes(b"12345")

    with pytest.raises(ValueError, match="size limit"):
        trusted_project_file(
            project_root=tmp_path,
            relative_path="configs/evaluation/policy.json",
            max_bytes=4,
        )
