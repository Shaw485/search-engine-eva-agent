from __future__ import annotations

from pathlib import Path

import pytest

from search_quality.agent_eval import catalog

ROOT = Path(__file__).resolve().parents[1]


def test_agent_eval_catalog_rejects_an_intermediate_symlink(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "stage5-retrieval-v1.json").write_bytes(
        (ROOT / "configs/agent-eval/stage5-retrieval-v1.json").read_bytes()
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "agent-eval").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        catalog.load_agent_eval_suite(project_root=tmp_path)


def test_agent_eval_catalog_reads_at_most_the_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_dir = tmp_path / "configs" / "agent-eval"
    suite_dir.mkdir(parents=True)
    path = suite_dir / "stage5-retrieval-v1.json"
    path.write_bytes(b"x" * 17)
    monkeypatch.setattr(catalog, "MAX_SUITE_BYTES", 16)

    with pytest.raises(ValueError, match="size limit"):
        catalog.load_agent_eval_suite(project_root=tmp_path)
