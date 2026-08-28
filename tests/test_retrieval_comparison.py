from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.evaluation.retrieval import run_query_scoped_retrieval
from search_quality.evaluation.retrieval_comparison import compare_retrieval_runs

ROOT = Path(__file__).resolve().parents[1]


def _rehash(run: dict) -> None:
    payload = {key: value for key, value in run.items() if key != "run_id"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    run["run_id"] = f"retrieval-{hashlib.sha256(canonical).hexdigest()[:12]}"


def _rehash_pipeline(run: dict) -> None:
    canonical = json.dumps(
        run["pipeline"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    run["pipeline_id"] = f"pipeline-{hashlib.sha256(canonical).hexdigest()[:12]}"
    _rehash(run)


@pytest.fixture(scope="module")
def runs() -> tuple[dict, dict]:
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=ROOT,
        manifest_path=ROOT / "data/manifests/esci-stage1.json",
    )
    policy_path = ROOT / "configs/evaluation/esci-primary-v1.json"
    policy = RelevancePolicy.from_path(policy_path)
    common = {
        "policy": policy,
        "policy_path": policy_path,
        "project_root": ROOT,
        "code_revision": "e" * 40,
    }
    return (
        run_query_scoped_retrieval(
            profile,
            pipeline_variant="title-exact-v1",
            **common,
        ),
        run_query_scoped_retrieval(
            profile,
            pipeline_variant="title-exact-multifield-v1",
            **common,
        ),
    )


def test_multi_field_candidate_improves_coverage_but_fails_quality_gates(
    runs: tuple[dict, dict],
) -> None:
    first = compare_retrieval_runs(*runs)
    second = compare_retrieval_runs(*runs)

    assert first == second
    assert first["comparison_id"].startswith("retrieval-comparison-")
    assert first["candidate_strategy"]["unique_relevant_contribution"] == 14
    assert first["aggregate_deltas"]["recall_union"]["judged_relevant_coverage"][
        "delta"
    ] == pytest.approx(0.03726951209709828)
    assert first["aggregate_deltas"]["fusion"]["judged_recall@10"]["delta"] > 0
    assert first["aggregate_deltas"]["fusion"]["ndcg@10"]["delta"] < 0
    assert first["aggregate_deltas"]["coarse_rank"]["mrr@10"]["delta"] < 0
    assert first["gate_result"]["passed"] is False
    failed = [
        item["name"] for item in first["gate_result"]["checks"] if not item["passed"]
    ]
    assert {
        "fusion_ndcg_at_10_floor",
        "fusion_mrr_at_10_floor",
        "coarse_ndcg_at_10_floor",
        "coarse_mrr_at_10_floor",
        "worst_query_coarse_ndcg_delta_floor",
        "worst_query_fusion_ndcg_delta_floor",
        "fusion_regressed_query_rate_ceiling",
    } <= set(failed)
    assert first["recommendation"] == "reject_candidate"
    assert first["next_action"] == "run_recall_channel_and_rrf_ablation"


def test_retrieval_comparison_rejects_incompatible_or_reversed_runs(
    runs: tuple[dict, dict],
) -> None:
    with pytest.raises(ValueError, match="baseline must use"):
        compare_retrieval_runs(runs[1], runs[0])

    changed = copy.deepcopy(runs[1])
    changed["code_revision"] = "f" * 40
    _rehash(changed)
    with pytest.raises(ValueError, match="matching code_revision"):
        compare_retrieval_runs(runs[0], changed)

    incomplete = copy.deepcopy(runs[1])
    incomplete["evaluation_boundary"]["denominator_complete"] = False
    _rehash(incomplete)
    with pytest.raises(ValueError, match="boundary"):
        compare_retrieval_runs(runs[0], incomplete)


def test_comparison_recomputes_aggregate_and_rejects_consistent_id_tampering(
    runs: tuple[dict, dict],
) -> None:
    changed = copy.deepcopy(runs[1])
    changed["aggregate"]["stages"]["rrf-v1"]["mean_ndcg@10"] = 1.0
    with pytest.raises(ValueError, match="content ID"):
        compare_retrieval_runs(runs[0], changed)

    _rehash(changed)
    with pytest.raises(ValueError, match="recomputed evidence"):
        compare_retrieval_runs(runs[0], changed)


def test_comparison_recomputes_channels_and_rejects_canonical_config_tampering(
    runs: tuple[dict, dict],
) -> None:
    changed_score = copy.deepcopy(runs[1])
    changed_score["per_query"][0]["rankings"]["recall_channels"][
        "title-bm25-recall-v1"
    ][0]["score"] = 999999.0
    _rehash(changed_score)
    with pytest.raises(ValueError, match="retriever"):
        compare_retrieval_runs(runs[0], changed_score)

    changed_config = copy.deepcopy(runs[1])
    multi_field_config = next(
        item
        for item in changed_config["pipeline"]["channels"]
        if item["channel_id"] == "multi-field-bm25-recall-v1"
    )
    multi_field_config["field_weights"]["title"] = 999.0
    _rehash_pipeline(changed_config)
    with pytest.raises(ValueError, match="implemented variant"):
        compare_retrieval_runs(runs[0], changed_config)


def test_comparison_requires_identical_query_pool_and_judgments(
    runs: tuple[dict, dict],
) -> None:
    changed = copy.deepcopy(runs[1])
    original = changed["per_query"][0]["judgments"][0]["product_title"]
    changed["per_query"][0]["judgments"][0]["product_title"] = original.swapcase()
    assert changed["per_query"][0]["judgments"][0]["product_title"] != original
    _rehash(changed)

    with pytest.raises(ValueError, match="identical Query pools and judgments"):
        compare_retrieval_runs(runs[0], changed)


def test_bounded_weight_ablation_selects_only_the_conservative_candidate() -> None:
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=ROOT,
        manifest_path=ROOT / "data/manifests/esci-stage1.json",
    )
    policy_path = ROOT / "configs/evaluation/esci-primary-v1.json"
    policy = RelevancePolicy.from_path(policy_path)
    common = {
        "policy": policy,
        "policy_path": policy_path,
        "project_root": ROOT,
        "code_revision": "9" * 40,
    }
    baseline = run_query_scoped_retrieval(
        profile, pipeline_variant="title-exact-v1", **common
    )
    conservative = run_query_scoped_retrieval(
        profile,
        pipeline_variant="title-exact-multifield-weighted-v1",
        **common,
    )
    aggressive = run_query_scoped_retrieval(
        profile,
        pipeline_variant="title-exact-multifield-weighted-aggressive-v1",
        **common,
    )

    accepted = compare_retrieval_runs(baseline, conservative)
    rejected = compare_retrieval_runs(baseline, aggressive)

    assert accepted["gate_result"]["passed"] is True
    assert accepted["candidate_strategy"]["fusion_weights"] == {
        "exact-title-recall-v1": 1.0,
        "multi-field-bm25-recall-v1": 0.1,
        "title-bm25-recall-v1": 1.0,
    }
    assert rejected["gate_result"]["passed"] is False
    assert {
        item["name"] for item in rejected["gate_result"]["checks"] if not item["passed"]
    } == {"fusion_mrr_at_10_floor", "fusion_regressed_query_rate_ceiling"}
