from __future__ import annotations

import random
from pathlib import Path

import polars as pl
import pytest

from search_quality.evaluation.baseline import run_candidate_title_bm25_baseline
from search_quality.evaluation.datasets import EvaluationProfile, sha256_file
from search_quality.evaluation.relevance import RelevancePolicy

PROJECT_ROOT = Path(__file__).parents[1]
SMOKE_PATH = PROJECT_ROOT / "data" / "samples" / "esci-stage1-smoke.parquet"
POLICY_PATH = PROJECT_ROOT / "configs" / "evaluation" / "esci-primary-v1.json"
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "esci-stage1.json"


def _smoke_profile() -> EvaluationProfile:
    return EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=PROJECT_ROOT,
        manifest_path=MANIFEST_PATH,
    )


def _synthetic_profile(path: Path, frame: pl.DataFrame) -> EvaluationProfile:
    return EvaluationProfile(
        profile_id="dev",
        path=path,
        file_sha256=sha256_file(path),
        canonical_sha256="synthetic-canonical-sha256",
        stage1_manifest_sha256="synthetic-manifest-sha256",
        stage1_schema_version="esci-stage1-v1",
        source_commit="synthetic-source-commit",
        expected_rows=frame.height,
        expected_queries=frame.select(
            pl.struct("product_locale", "query_id").n_unique()
        ).item(),
        expected_products=frame.select(
            pl.struct("product_locale", "product_id").n_unique()
        ).item(),
    )


def test_smoke_baseline_is_complete_deterministic_and_bounded() -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    profile = _smoke_profile()
    first = run_candidate_title_bm25_baseline(
        profile, policy=policy, code_revision="test-revision"
    )
    second = run_candidate_title_bm25_baseline(
        profile, policy=policy, code_revision="test-revision"
    )

    assert first == second
    assert first["dataset"] == {
        **profile.to_manifest_dict(),
        "eval_splits": ["dev"],
        "judgments": 416,
        "locales": ["us"],
        "origin_splits": ["train"],
        "products": 416,
        "queries": 20,
    }
    assert first["metrics"]["ndcg@10"] == pytest.approx(0.7190978854121438)
    assert first["metrics"]["mrr@10"] == pytest.approx(0.8516666666666668)
    assert first["metrics"]["success@1"] == pytest.approx(0.75)
    assert first["metrics"]["success@5"] == pytest.approx(1.0)
    assert len(first["per_query"]) == 20
    assert all(0.0 <= value <= 1.0 for value in first["metrics"].values())

    for query in first["per_query"]:
        ranked_ids = [item["product_id"] for item in query["ranking"]]
        assert len(ranked_ids) == query["candidate_count"]
        assert len(ranked_ids) == len(set(ranked_ids))

    for metric_name, aggregate in first["metrics"].items():
        expected = sum(
            query["metrics"][metric_name] for query in first["per_query"]
        ) / len(first["per_query"])
        assert aggregate == pytest.approx(expected)


def test_routine_baseline_rejects_relabelled_official_test_data(
    tmp_path: Path,
) -> None:
    frame = (
        pl.read_parquet(SMOKE_PATH)
        .head(1)
        .with_columns(
            pl.lit(False).alias("is_smoke"),
            pl.lit("test").alias("origin_split"),
        )
    )
    path = tmp_path / "test.parquet"
    frame.write_parquet(path)
    policy = RelevancePolicy.from_path(POLICY_PATH)
    profile = _synthetic_profile(path, frame)
    with pytest.raises(ValueError, match="official-train-derived"):
        run_candidate_title_bm25_baseline(
            profile, policy=policy, code_revision="test-revision"
        )


def test_baseline_is_independent_of_input_row_order(tmp_path: Path) -> None:
    frame = pl.read_parquet(SMOKE_PATH).with_columns(pl.lit(False).alias("is_smoke"))
    rows = list(frame.iter_rows(named=True))
    random.Random(17).shuffle(rows)
    shuffled_path = tmp_path / "shuffled.parquet"
    pl.DataFrame(rows, schema=frame.schema).write_parquet(shuffled_path)

    policy = RelevancePolicy.from_path(POLICY_PATH)
    ordered = run_candidate_title_bm25_baseline(
        _smoke_profile(), policy=policy, code_revision="test-revision"
    )
    shuffled = run_candidate_title_bm25_baseline(
        _synthetic_profile(shuffled_path, pl.DataFrame(rows, schema=frame.schema)),
        policy=policy,
        code_revision="test-revision",
    )
    assert shuffled["metrics"] == ordered["metrics"]
    assert shuffled["per_query"] == ordered["per_query"]


def test_query_without_relevant_products_still_counts_in_macro_average(
    tmp_path: Path,
) -> None:
    frame = pl.DataFrame(
        {
            "query_id": [1, 2],
            "example_id": [11, 22],
            "query_text": ["mouse", "case"],
            "product_id": ["p1", "p2"],
            "product_locale": ["us", "us"],
            "product_title": ["Mouse", "Case"],
            "esci_label": ["E", "I"],
            "eval_split": ["dev", "dev"],
            "origin_split": ["train", "train"],
            "is_smoke": [False, False],
        }
    )
    path = tmp_path / "dev.parquet"
    frame.write_parquet(path)
    policy = RelevancePolicy.from_path(POLICY_PATH)
    run = run_candidate_title_bm25_baseline(
        _synthetic_profile(path, frame),
        policy=policy,
        code_revision="test-revision",
    )
    assert run["metrics"]["ndcg@10"] == pytest.approx(0.5)
    assert run["metrics"]["mrr@10"] == pytest.approx(0.5)
    assert run["metrics"]["success@1"] == pytest.approx(0.5)
