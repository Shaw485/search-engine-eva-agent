"""Orchestrate and persist one bounded stage-aware retrieval analysis."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from search_quality.evaluation.artifacts import (
    require_clean_code_revision,
    write_immutable_json,
)
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.evaluation.retrieval import run_query_scoped_retrieval
from search_quality.evaluation.retrieval_comparison import compare_retrieval_runs

from .stage_diagnosis import diagnose_retrieval_stages

logger = logging.getLogger("search_quality.retrieval_analysis")


def _resolve_artifact_root(
    project_root: Path,
    artifact_root: str | Path | None,
) -> Path:
    if artifact_root is None:
        return project_root / "runs"
    requested = Path(artifact_root)
    if not requested.is_absolute():
        raise ValueError("retrieval artifact root must be an absolute path")
    resolved = requested.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("retrieval artifact root must be a directory")
    return resolved


def generate_retrieval_analysis(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    profile_id: Literal["smoke"] = "smoke",
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> dict[str, Any]:
    """Run the fixed smoke pipeline and return an authenticated workbench summary."""

    if profile_id != "smoke":
        raise ValueError("retrieval stage analysis is currently smoke-only")
    root = Path(project_root).resolve(strict=True)
    run_store = _resolve_artifact_root(root, artifact_root)
    manifest_path = root / "data" / "manifests" / "esci-stage1.json"
    policy_path = root / "configs" / "evaluation" / "esci-primary-v1.json"
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id=profile_id,
        project_root=root,
        manifest_path=manifest_path,
    )
    policy = RelevancePolicy.from_path(policy_path)
    revision = revision_provider(root)
    baseline = run_query_scoped_retrieval(
        profile,
        policy=policy,
        policy_path=policy_path,
        project_root=root,
        code_revision=revision,
        pipeline_variant="title-exact-v1",
    )
    diagnosis = diagnose_retrieval_stages(baseline)
    experiments = []
    for variant in (
        "title-exact-multifield-v1",
        "title-exact-multifield-weighted-v1",
        "title-exact-multifield-weighted-aggressive-v1",
    ):
        candidate = run_query_scoped_retrieval(
            profile,
            policy=policy,
            policy_path=policy_path,
            project_root=root,
            code_revision=revision,
            pipeline_variant=variant,
        )
        candidate_diagnosis = diagnose_retrieval_stages(candidate)
        comparison = compare_retrieval_runs(baseline, candidate)
        experiments.append(
            {
                "candidate": candidate,
                "comparison": comparison,
                "diagnosis": candidate_diagnosis,
            }
        )
    passing = [
        experiment
        for experiment in experiments
        if experiment["comparison"]["gate_result"]["passed"] is True
    ]
    selection_pool = passing or experiments
    selected = max(
        selection_pool,
        key=lambda experiment: (
            min(
                item["fusion_ndcg@10_delta"]
                for item in experiment["comparison"]["per_query"]
            ),
            experiment["comparison"]["aggregate_deltas"]["coarse_rank"]["ndcg@10"][
                "delta"
            ],
            experiment["candidate"]["pipeline"]["variant"],
        ),
    )
    candidate = selected["candidate"]
    candidate_diagnosis = selected["diagnosis"]
    comparison = selected["comparison"]
    write_immutable_json(
        run_store / "retrieval-runs" / f"{baseline['run_id']}.json",
        baseline,
    )
    write_immutable_json(
        run_store / "stage-diagnoses" / f"{diagnosis['diagnosis_id']}.json",
        diagnosis,
    )
    for experiment in experiments:
        experiment_candidate = experiment["candidate"]
        experiment_diagnosis = experiment["diagnosis"]
        experiment_comparison = experiment["comparison"]
        write_immutable_json(
            run_store / "retrieval-runs" / f"{experiment_candidate['run_id']}.json",
            experiment_candidate,
        )
        write_immutable_json(
            run_store
            / "stage-diagnoses"
            / f"{experiment_diagnosis['diagnosis_id']}.json",
            experiment_diagnosis,
        )
        write_immutable_json(
            run_store
            / "retrieval-comparisons"
            / f"{experiment_comparison['comparison_id']}.json",
            experiment_comparison,
        )
    passed = comparison["gate_result"]["passed"] is True
    result = {
        "aggregate": baseline["aggregate"],
        "candidate_aggregate": candidate["aggregate"],
        "candidate_diagnosis": candidate_diagnosis,
        "candidate_diagnosis_id": candidate_diagnosis["diagnosis_id"],
        "candidate_run_id": candidate["run_id"],
        "comparison": comparison,
        "comparison_id": comparison["comparison_id"],
        "diagnosis": diagnosis,
        "diagnosis_id": diagnosis["diagnosis_id"],
        "evaluation_boundary": baseline["evaluation_boundary"],
        "experiments": [
            {
                "candidate_run_id": experiment["candidate"]["run_id"],
                "comparison_id": experiment["comparison"]["comparison_id"],
                "failed_gates": [
                    check["name"]
                    for check in experiment["comparison"]["gate_result"]["checks"]
                    if not check["passed"]
                ],
                "fusion_mrr_at_10_delta": experiment["comparison"]["aggregate_deltas"][
                    "fusion"
                ]["mrr@10"]["delta"],
                "fusion_ndcg_at_10_delta": experiment["comparison"]["aggregate_deltas"][
                    "fusion"
                ]["ndcg@10"]["delta"],
                "fusion_weights": experiment["candidate"]["pipeline"]["fusion"][
                    "weights"
                ],
                "gate_passed": experiment["comparison"]["gate_result"]["passed"],
                "pipeline_variant": experiment["candidate"]["pipeline"]["variant"],
                "worst_fusion_query_ndcg_at_10_delta": min(
                    item["fusion_ndcg@10_delta"]
                    for item in experiment["comparison"]["per_query"]
                ),
            }
            for experiment in experiments
        ],
        "pipeline": baseline["pipeline"],
        "pipeline_id": baseline["pipeline_id"],
        "profile": profile_id,
        "proposal": {
            "candidate_strategy_id": (
                "multi-field-bm25-weighted-rrf-v1"
                if passed
                else "multi-field-bm25-recall-v1"
            ),
            "decision": "request_owner_review" if passed else "reject_candidate",
            "next_action": comparison["next_action"],
            "reason": (
                "A bounded RRF weight ablation preserved final quality while expanding closed-pool coverage."
                if passed
                else "The channel recovered relevant products, but no bounded fusion candidate passed all gates."
            ),
        },
        "retrieval_run_id": baseline["run_id"],
        "schema_version": "retrieval-stage-analysis-response-v1",
        "status": "proposal_ready" if passed else "no_safe_improvement",
    }
    logger.info(
        "retrieval_analysis_artifacts_stored",
        extra={
            "diagnosis_id": diagnosis["diagnosis_id"],
            "candidate_run_id": candidate["run_id"],
            "comparison_id": comparison["comparison_id"],
            "experiment_count": len(experiments),
            "pipeline_run_id": baseline["run_id"],
            "profile_id": profile_id,
        },
    )
    return result
