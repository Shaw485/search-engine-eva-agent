from __future__ import annotations

import pytest

from search_quality.evaluation.authorization import ensure_profile_authorized
from search_quality.evaluation.baseline import run_candidate_baseline
from search_quality.evaluation.cli import build_parser, ensure_profile_unlocked
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy


def test_cli_defaults_to_all_smoke_comparators() -> None:
    args = build_parser().parse_args([])
    assert args.profile == "smoke"
    assert args.ranker == "all"


def test_cli_keeps_frozen_test_unreachable() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--profile", "test"])


def test_dev_profile_is_locked_until_owner_checkpoint_is_recorded() -> None:
    ensure_profile_unlocked("smoke")
    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        ensure_profile_unlocked("dev")


def test_shared_baseline_api_rejects_dev_before_reading_data(tmp_path) -> None:
    missing = tmp_path / "must-not-be-opened.parquet"
    profile = EvaluationProfile(
        profile_id="dev",
        path=missing,
        file_sha256="unused-file-hash",
        canonical_sha256="unused-canonical-hash",
        stage1_manifest_sha256="unused-manifest-hash",
        stage1_schema_version="esci-stage1-v1",
        source_commit="unused-source-commit",
        expected_rows=1,
        expected_queries=1,
        expected_products=1,
    )

    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        run_candidate_baseline(
            profile,
            policy=RelevancePolicy(
                policy_id="test",
                label_gains={"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.0},
                relevant_labels=frozenset({"E", "S"}),
            ),
            code_revision="test-revision",
            ranker_name="random",
        )
    assert not missing.exists()
    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        ensure_profile_authorized("dev")
