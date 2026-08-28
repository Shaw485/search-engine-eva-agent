from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from search_quality.agent.stage_diagnosis import (
    StageFinding,
    diagnose_retrieval_stages,
)
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.evaluation.retrieval import run_query_scoped_retrieval
from search_quality.observability import configure_logging, logging_context

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


@pytest.fixture(scope="module")
def retrieval_run() -> dict:
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=ROOT,
        manifest_path=ROOT / "data/manifests/esci-stage1.json",
    )
    policy_path = ROOT / "configs/evaluation/esci-primary-v1.json"
    return run_query_scoped_retrieval(
        profile,
        policy=RelevancePolicy.from_path(policy_path),
        policy_path=policy_path,
        project_root=ROOT,
        code_revision="b" * 40,
    )


def test_real_stage_diagnosis_selects_evidence_backed_engineering_work(
    retrieval_run: dict,
) -> None:
    first = diagnose_retrieval_stages(retrieval_run)
    second = diagnose_retrieval_stages(retrieval_run)

    assert first == second
    assert first["status"] == "diagnosable"
    assert first["primary_category"] == "recall"
    assert first["recommended_next_action"] == ("run_independent_recall_experiment")
    assert [item["subtype"] for item in first["findings"]] == [
        "known_relevant_missing_from_all_channels",
        "no_unique_relevant_coverage",
        "fusion_quality_regression",
    ]
    assert [item["strategy_family"] for item in first["strategy_options"]] == [
        "independent_recall_channel",
        "recall_channel_ablation",
        "rrf_channel_weight_ablation",
    ]
    assert first["strategy_options"][0]["availability"] == "implemented"
    assert first["unavailable_stages"] == [
        "fine_rank",
        "rerank",
        "query_understanding_gold",
    ]


def test_stage_diagnosis_rejects_aggregate_only_coarse_tampering(
    retrieval_run: dict,
) -> None:
    damaged = copy.deepcopy(retrieval_run)
    fusion = damaged["aggregate"]["stages"]["rrf-v1"]["mean_ndcg@10"]
    damaged["aggregate"]["stages"]["coarse-title-bm25-v1"]["mean_ndcg@10"] = (
        fusion - 0.05
    )

    with pytest.raises(ValueError, match="content ID"):
        diagnose_retrieval_stages(damaged)

    _rehash(damaged)
    with pytest.raises(ValueError, match="recomputed evidence"):
        diagnose_retrieval_stages(damaged)


def test_stage_diagnosis_rejects_incomplete_boundary_and_corrupt_lineage(
    retrieval_run: dict,
) -> None:
    incomplete = copy.deepcopy(retrieval_run)
    incomplete["evaluation_boundary"]["denominator_complete"] = False
    _rehash(incomplete)
    with pytest.raises(ValueError, match="boundary"):
        diagnose_retrieval_stages(incomplete)

    corrupt = copy.deepcopy(retrieval_run)
    corrupt["per_query"][0]["lineage"][0]["first_loss_stage"] = "fusion"
    _rehash(corrupt)
    with pytest.raises(ValueError, match="lineage"):
        diagnose_retrieval_stages(corrupt)


def test_stage_diagnosis_output_and_logs_omit_raw_search_evidence(
    retrieval_run: dict,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"stage_diagnosis": "INFO"},
        stream=stream,
    )
    with logging_context(trace_id="diagnosis-safe-trace"):
        result = diagnose_retrieval_stages(retrieval_run)

    serialized = json.dumps(result, ensure_ascii=False).lower()
    logs = stream.getvalue().lower()
    private_query = retrieval_run["per_query"][0]["query_text"].lower()
    private_product = retrieval_run["per_query"][0]["lineage"][0]["product_id"].lower()
    assert private_query not in serialized
    assert private_product not in serialized
    assert private_query not in logs
    assert private_product not in logs
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert {event["module"] for event in events} == {"stage_diagnosis"}
    assert all(event["trace_id"] == "diagnosis-safe-trace" for event in events)


def test_stage_finding_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        StageFinding.model_validate(
            {
                "category": "recall",
                "evidence_refs": ["run:retrieval-aaaaaaaaaaaa"],
                "finding_id": "finding-aaaaaaaaaaaa",
                "impact": 0.2,
                "impact_aggregation": "relevant_item_micro_rate",
                "stage_dropped_relevant_count": 1,
                "subtype": "known_relevant_missing",
                "unknown": "no",
                "verdict": "confirmed",
            }
        )
