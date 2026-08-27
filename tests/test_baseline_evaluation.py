from __future__ import annotations

import io
import json
import random
from pathlib import Path

import polars as pl
import pytest

from search_quality.evaluation.baseline import (
    RANKER_NAMES,
    _validate_ranking,
    run_candidate_baseline,
    run_candidate_random_baseline,
    run_candidate_title_bm25_baseline,
)
from search_quality.evaluation.datasets import EvaluationProfile, sha256_file
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.observability import configure_logging, logging_context
from search_quality.ranking import RankedProduct

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
        profile_id="smoke",
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


@pytest.mark.parametrize(
    ("ranked", "message"),
    [
        (
            [
                RankedProduct("us", "p1", 1.0, 1),
                RankedProduct("us", "p1", 0.5, 2),
            ],
            "duplicate",
        ),
        ([RankedProduct("us", "p1", 1.0, 1)], "does not match"),
        (
            [
                RankedProduct("us", "p1", 1.0, 2),
                RankedProduct("us", "p2", 0.5, 1),
            ],
            "non-contiguous",
        ),
        (
            [
                RankedProduct("us", "p1", float("nan"), 1),
                RankedProduct("us", "p2", 0.5, 2),
            ],
            "non-finite",
        ),
    ],
)
def test_harness_rejects_invalid_ranker_output(
    ranked: list[RankedProduct], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_ranking([("us", "p1"), ("us", "p2")], ranked)


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


@pytest.mark.parametrize("ranker_name", RANKER_NAMES)
def test_every_smoke_comparator_is_deterministic_complete_and_label_blind(
    ranker_name: str,
) -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    profile = _smoke_profile()
    first = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision="test-revision",
        ranker_name=ranker_name,
    )
    second = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision="test-revision",
        ranker_name=ranker_name,
    )

    assert first == second
    assert first["ranker"]["ranker_id"].startswith("candidate-")
    assert first["evaluation_boundary"] == {
        "full_catalog_recall_claimed": False,
        "task": "judged-candidate-reranking",
        "unjudged_products_are_irrelevant": False,
    }
    assert len(first["per_query"]) == 20
    for query in first["per_query"]:
        ranking = query["ranking"]
        assert [item["rank"] for item in ranking] == list(
            range(1, query["candidate_count"] + 1)
        )
        assert len({(item["locale"], item["product_id"]) for item in ranking}) == len(
            ranking
        )


def test_random_seed_is_recorded_and_changes_run_identity() -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    profile = _smoke_profile()
    first = run_candidate_random_baseline(
        profile,
        policy=policy,
        code_revision="test-revision",
        seed=17,
    )
    second = run_candidate_random_baseline(
        profile,
        policy=policy,
        code_revision="test-revision",
        seed=18,
    )

    assert first["ranker"]["seed"] == 17
    assert second["ranker"]["seed"] == 18
    assert first["run_id"] != second["run_id"]


def test_evaluation_logs_trace_run_and_metrics_without_query_text() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"evaluation": "INFO", "ranking": "OFF"},
        stream=stream,
    )
    with logging_context(trace_id="evaluation-trace"):
        run = run_candidate_baseline(
            _smoke_profile(),
            policy=RelevancePolicy.from_path(POLICY_PATH),
            code_revision="test-revision",
            ranker_name="keyword-overlap",
        )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "baseline_started",
        "baseline_completed",
    ]
    assert events[-1]["trace_id"] == "evaluation-trace"
    assert events[-1]["run_id"] == run["run_id"]
    assert events[-1]["metrics"] == run["metrics"]
    assert "query_text" not in stream.getvalue()


def test_ranking_debug_can_be_isolated_from_evaluation_logs() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"ranking": "DEBUG"},
        stream=stream,
    )
    with logging_context(trace_id="ranking-trace"):
        run_candidate_baseline(
            _smoke_profile(),
            policy=RelevancePolicy.from_path(POLICY_PATH),
            code_revision="test-revision",
            ranker_name="random",
        )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    query_events = [event for event in events if event["event"] == "query_ranked"]
    assert len(query_events) == 20
    assert all(event["module"] == "ranking" for event in events)
    assert all(event["trace_id"] == "ranking-trace" for event in events)
    assert all("query_id" in event for event in query_events)
    assert "query_text" not in stream.getvalue()


@pytest.mark.parametrize("ranker_name", RANKER_NAMES)
def test_routine_baseline_rejects_relabelled_official_test_data(
    tmp_path: Path,
    ranker_name: str,
) -> None:
    frame = (
        pl.read_parquet(SMOKE_PATH)
        .head(1)
        .with_columns(
            pl.lit(True).alias("is_smoke"),
            pl.lit("test").alias("origin_split"),
        )
    )
    path = tmp_path / "test.parquet"
    frame.write_parquet(path)
    policy = RelevancePolicy.from_path(POLICY_PATH)
    profile = _synthetic_profile(path, frame)
    with pytest.raises(ValueError, match="official-train-derived"):
        run_candidate_baseline(
            profile,
            policy=policy,
            code_revision="test-revision",
            ranker_name=ranker_name,
        )


def test_baseline_is_independent_of_input_row_order(tmp_path: Path) -> None:
    frame = pl.read_parquet(SMOKE_PATH)
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
            "is_smoke": [True, True],
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
