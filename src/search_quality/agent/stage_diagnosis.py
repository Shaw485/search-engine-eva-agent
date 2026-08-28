"""Evidence-only diagnosis of retrieval, fusion and ranking stage bottlenecks."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from search_quality.evaluation.retrieval_validation import validate_retrieval_run

from .contracts import StrictModel

logger = logging.getLogger("search_quality.stage_diagnosis")

DIAGNOSIS_SCHEMA_VERSION = "stage-diagnosis-v1"
RUN_ID_PATTERN = r"retrieval-[0-9a-f]{12}"
DIAGNOSIS_ID_PATTERN = r"stage-diagnosis-[0-9a-f]{12}"
FINDING_ID_PATTERN = r"finding-[0-9a-f]{12}"
FiniteUnitFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0),
]

StageCategory = Literal[
    "recall",
    "fusion",
    "coarse_rank",
    "post_retrieval_ranking",
    "data_or_labels",
]


class StageFinding(StrictModel):
    finding_id: StrictStr = Field(pattern=rf"^{FINDING_ID_PATTERN}$")
    category: StageCategory
    subtype: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    verdict: Literal["confirmed", "suspected", "blocked"]
    stage_dropped_relevant_count: StrictInt = Field(ge=0)
    impact: FiniteUnitFloat
    impact_aggregation: Literal["relevant_item_micro_rate", "mean_query_metric_delta"]
    evidence_refs: list[StrictStr] = Field(min_length=1, max_length=8)


class StrategyOption(StrictModel):
    strategy_family: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    target_stage: StageCategory
    availability: Literal[
        "implemented",
        "requires_engineering",
        "requires_owner_review",
        "blocked_insufficient_evidence",
    ]
    hypothesis: StrictStr = Field(min_length=1, max_length=1000)
    supporting_finding_ids: list[StrictStr] = Field(min_length=1, max_length=8)
    required_experiment: StrictStr = Field(min_length=1, max_length=1000)


class QueryStageDiagnosis(StrictModel):
    query_id: StrictStr = Field(pattern=r"^[0-9]+$")
    status: Literal["diagnosable", "no_failure", "insufficient_evidence"]
    primary_category: StageCategory | None
    first_loss_counts: dict[Literal["recall", "fusion", "coarse_rank", "retained"], int]
    fusion_ndcg_at_10: FiniteUnitFloat
    coarse_ndcg_at_10: FiniteUnitFloat
    coarse_ndcg_delta_at_10: Annotated[
        float,
        Field(strict=True, allow_inf_nan=False, ge=-1.0, le=1.0),
    ]

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "no_failure" and self.primary_category is not None:
            raise ValueError("no_failure Query cannot have a primary category")
        if self.status == "diagnosable" and self.primary_category is None:
            raise ValueError("diagnosable Query needs a primary category")
        return self


class StageDiagnosis(StrictModel):
    schema_version: Literal["stage-diagnosis-v1"] = DIAGNOSIS_SCHEMA_VERSION
    diagnosis_id: StrictStr = Field(pattern=rf"^{DIAGNOSIS_ID_PATTERN}$")
    pipeline_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    status: Literal[
        "diagnosable",
        "no_failure",
        "insufficient_evidence",
        "requires_engineering",
    ]
    primary_category: StageCategory | None
    recommended_next_action: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    findings: list[StageFinding] = Field(max_length=8)
    strategy_options: list[StrategyOption] = Field(max_length=8)
    per_query: list[QueryStageDiagnosis] = Field(min_length=1)
    observed_stages: list[StrictStr]
    unavailable_stages: list[StrictStr]
    forbidden_claims: list[StrictStr]

    @model_validator(mode="after")
    def validate_findings_and_options(self):
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding IDs must be unique")
        known = set(finding_ids)
        for option in self.strategy_options:
            if not set(option.supporting_finding_ids) <= known:
                raise ValueError("strategy option references an unknown finding")
        if self.status == "no_failure" and self.primary_category is not None:
            raise ValueError("no_failure diagnosis cannot have a primary category")
        return self


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(canonical).hexdigest()[:12]}"


def _finding(
    *,
    run_id: str,
    category: StageCategory,
    subtype: str,
    verdict: Literal["confirmed", "suspected", "blocked"],
    stage_dropped: int,
    impact: float,
    impact_aggregation: Literal["relevant_item_micro_rate", "mean_query_metric_delta"],
) -> StageFinding:
    body = {
        "category": category,
        "evidence_refs": [f"run:{run_id}"],
        "impact": round(impact, 12),
        "impact_aggregation": impact_aggregation,
        "stage_dropped_relevant_count": stage_dropped,
        "subtype": subtype,
        "verdict": verdict,
    }
    return StageFinding(
        finding_id=_stable_id("finding", body),
        **body,
    )


def _metric(run: dict[str, Any], stage_id: str, metric: str) -> float:
    try:
        value = run["aggregate"]["stages"][stage_id][metric]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"retrieval Run is missing {stage_id}.{metric}") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("stage aggregate metric must be numeric")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("stage aggregate metric must be finite and in [0, 1]")
    return value


def _validate_run(run: dict[str, Any]) -> tuple[Counter[str], int]:
    if not isinstance(run, dict):
        raise TypeError("retrieval Run must be an object")
    if run.get("schema_version") != "query-scoped-retrieval-run-v1":
        raise ValueError("unsupported retrieval Run schema")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(RUN_ID_PATTERN, run_id):
        raise ValueError("invalid retrieval Run ID")
    boundary = run.get("evaluation_boundary")
    if not isinstance(boundary, dict):
        raise ValueError("retrieval Run is missing its evaluation boundary")
    required_boundary = {
        "denominator_complete": True,
        "full_catalog_recall_claimed": False,
        "shared_corpus_recall_claimed": False,
        "task": "query-scoped-judged-pool-candidate-retention",
        "unjudged_products_are_irrelevant": False,
    }
    if any(boundary.get(key) != value for key, value in required_boundary.items()):
        raise ValueError("retrieval Run boundary is not eligible for stage diagnosis")
    per_query = run.get("per_query")
    if not isinstance(per_query, list) or not per_query:
        raise ValueError("retrieval Run needs per-Query evidence")
    counts: Counter[str] = Counter()
    total_relevant = 0
    seen_query_ids: set[str] = set()
    for query in per_query:
        if not isinstance(query, dict):
            raise ValueError("per-Query evidence must contain objects")
        query_id = str(query.get("query_id", ""))
        if not query_id.isdigit() or query_id in seen_query_ids:
            raise ValueError("per-Query evidence has an invalid or duplicate Query ID")
        seen_query_ids.add(query_id)
        relevant_count = query.get("relevant_count")
        if (
            isinstance(relevant_count, bool)
            or not isinstance(relevant_count, int)
            or relevant_count < 1
        ):
            raise ValueError("every Query needs at least one judged relevant product")
        lineage = query.get("lineage")
        if not isinstance(lineage, list) or len(lineage) != relevant_count:
            raise ValueError("relevant lineage must cover every relevant product")
        keys: set[tuple[str, str]] = set()
        for item in lineage:
            if not isinstance(item, dict):
                raise ValueError("lineage items must be objects")
            key = (str(item.get("locale", "")), str(item.get("product_id", "")))
            if not all(key) or key in keys:
                raise ValueError("lineage product keys must be non-empty and unique")
            keys.add(key)
            loss_stage = item.get("first_loss_stage")
            if loss_stage not in {"recall", "fusion", "coarse_rank", "retained"}:
                raise ValueError("lineage has an invalid first-loss stage")
            counts[loss_stage] += 1
        total_relevant += relevant_count
    declared_counts = run.get("aggregate", {}).get("first_loss_counts")
    if not isinstance(declared_counts, dict) or any(
        declared_counts.get(stage) != counts.get(stage, 0)
        for stage in ("recall", "fusion", "coarse_rank", "retained")
    ):
        raise ValueError("aggregate first-loss counts do not match lineage")
    return counts, total_relevant


def diagnose_retrieval_stages(run: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic stage findings without reading raw source data."""

    supplied_run_id = run.get("run_id") if isinstance(run, dict) else None
    run_id = (
        supplied_run_id
        if isinstance(supplied_run_id, str)
        and re.fullmatch(RUN_ID_PATTERN, supplied_run_id)
        else "invalid"
    )
    logger.info("stage_diagnosis_started", extra={"pipeline_run_id": run_id})
    try:
        validate_retrieval_run(run, role="stage diagnosis")
        counts, total_relevant = _validate_run(run)
        channel_ids = [
            str(channel["channel_id"]) for channel in run["pipeline"]["channels"]
        ]
        channel_ndcg = {
            channel_id: _metric(run, channel_id, "mean_ndcg@10")
            for channel_id in channel_ids
        }
        best_channel_id, best_channel_ndcg = max(
            channel_ndcg.items(), key=lambda item: (item[1], item[0])
        )
        fusion_ndcg = _metric(run, "rrf-v1", "mean_ndcg@10")
        coarse_ndcg = _metric(
            run,
            "coarse-title-bm25-v1",
            "mean_ndcg@10",
        )
        exact_unique = run["aggregate"].get("exact_unique_relevant_count")
        if (
            isinstance(exact_unique, bool)
            or not isinstance(exact_unique, int)
            or exact_unique < 0
        ):
            raise ValueError("exact unique relevant count must be non-negative")

        findings: list[StageFinding] = []
        if counts["recall"]:
            findings.append(
                _finding(
                    run_id=run_id,
                    category="recall",
                    subtype="known_relevant_missing_from_all_channels",
                    verdict="confirmed",
                    stage_dropped=counts["recall"],
                    impact=counts["recall"] / total_relevant,
                    impact_aggregation="relevant_item_micro_rate",
                )
            )
        if exact_unique == 0:
            findings.append(
                _finding(
                    run_id=run_id,
                    category="recall",
                    subtype="no_unique_relevant_coverage",
                    verdict="suspected",
                    stage_dropped=0,
                    impact=0.0,
                    impact_aggregation="relevant_item_micro_rate",
                )
            )
        if fusion_ndcg < best_channel_ndcg - 1e-12:
            findings.append(
                _finding(
                    run_id=run_id,
                    category="fusion",
                    subtype="fusion_quality_regression",
                    verdict="confirmed",
                    stage_dropped=counts["fusion"],
                    impact=best_channel_ndcg - fusion_ndcg,
                    impact_aggregation="mean_query_metric_delta",
                )
            )
        if coarse_ndcg < fusion_ndcg - 1e-12:
            findings.append(
                _finding(
                    run_id=run_id,
                    category="coarse_rank",
                    subtype="coarse_rank_quality_regression",
                    verdict="confirmed",
                    stage_dropped=counts["coarse_rank"],
                    impact=fusion_ndcg - coarse_ndcg,
                    impact_aggregation="mean_query_metric_delta",
                )
            )

        query_diagnoses: list[QueryStageDiagnosis] = []
        for query in run["per_query"]:
            losses = Counter(item["first_loss_stage"] for item in query["lineage"])
            query_fusion_ndcg = float(query["metrics"]["fusion"]["ndcg@10"])
            query_coarse_ndcg = float(query["metrics"]["coarse_rank"]["ndcg@10"])
            delta = query_coarse_ndcg - query_fusion_ndcg
            if losses["recall"]:
                primary: StageCategory | None = "recall"
            elif losses["fusion"]:
                primary = "fusion"
            elif delta < -1e-12:
                primary = "coarse_rank"
            else:
                primary = None
            query_diagnoses.append(
                QueryStageDiagnosis(
                    query_id=str(query["query_id"]),
                    status="diagnosable" if primary is not None else "no_failure",
                    primary_category=primary,
                    first_loss_counts={
                        stage: losses.get(stage, 0)
                        for stage in ("recall", "fusion", "coarse_rank", "retained")
                    },
                    fusion_ndcg_at_10=query_fusion_ndcg,
                    coarse_ndcg_at_10=query_coarse_ndcg,
                    coarse_ndcg_delta_at_10=delta,
                )
            )

        options: list[StrategyOption] = []
        by_subtype = {finding.subtype: finding for finding in findings}
        recall_finding = by_subtype.get("known_relevant_missing_from_all_channels")
        if recall_finding is not None:
            options.append(
                StrategyOption(
                    strategy_family="independent_recall_channel",
                    target_stage="recall",
                    availability="implemented",
                    hypothesis=(
                        "A channel using a different signal, such as additional fields, "
                        "normalization or semantic retrieval, may recover judged relevant "
                        "products with no current title-token hit."
                    ),
                    supporting_finding_ids=[recall_finding.finding_id],
                    required_experiment=(
                        "Implement one label-blind channel, rerun the same closed pools, "
                        "and require positive unique relevant contribution before keeping it."
                    ),
                )
            )
        redundant = by_subtype.get("no_unique_relevant_coverage")
        if redundant is not None:
            options.append(
                StrategyOption(
                    strategy_family="recall_channel_ablation",
                    target_stage="recall",
                    availability="implemented",
                    hypothesis=(
                        "The exact-title channel adds no uniquely relevant product in this "
                        "Run; only an ablation can determine whether it still helps order."
                    ),
                    supporting_finding_ids=[redundant.finding_id],
                    required_experiment=(
                        "Compare the current pipeline, multi-field-only and title plus "
                        "multi-field variants under the same Recall and nDCG cutoffs."
                    ),
                )
            )
        fusion_finding = by_subtype.get("fusion_quality_regression")
        if fusion_finding is not None:
            options.append(
                StrategyOption(
                    strategy_family="rrf_channel_weight_ablation",
                    target_stage="fusion",
                    availability="implemented",
                    hypothesis=(
                        f"Uniform RRF is below the best observed single channel "
                        f"({best_channel_id}); fixed channel and weight ablations can test "
                        "whether fusion, rather than recall coverage, is the bottleneck."
                    ),
                    supporting_finding_ids=[fusion_finding.finding_id],
                    required_experiment=(
                        "Run fixed BM25-only, uniform-RRF and bounded weighted-RRF variants; "
                        "retain fusion only if it adds coverage or passes quality gates."
                    ),
                )
            )
        coarse_finding = by_subtype.get("coarse_rank_quality_regression")
        if coarse_finding is not None:
            options.append(
                StrategyOption(
                    strategy_family="coarse_rank_ablation",
                    target_stage="coarse_rank",
                    availability="requires_engineering",
                    hypothesis="The current coarse scorer may discard or demote useful fused candidates.",
                    supporting_finding_ids=[coarse_finding.finding_id],
                    required_experiment=(
                        "Compare fusion handoff with and without coarse ranking at identical "
                        "final K and inspect affected relevant lineage."
                    ),
                )
            )

        primary = findings[0].category if findings else None
        if recall_finding is not None:
            next_action = "run_independent_recall_experiment"
        elif fusion_finding is not None:
            next_action = "run_fusion_ablation"
        elif coarse_finding is not None:
            next_action = "run_coarse_rank_ablation"
        else:
            next_action = "no_strategy_change"
        body = {
            "findings": [item.model_dump(mode="json") for item in findings],
            "forbidden_claims": list(run["evaluation_boundary"]["forbidden_claims"]),
            "observed_stages": [
                "recall_channels",
                "recall_union",
                "fusion",
                "coarse_rank",
            ],
            "per_query": [item.model_dump(mode="json") for item in query_diagnoses],
            "pipeline_run_id": run_id,
            "primary_category": primary,
            "recommended_next_action": next_action,
            "schema_version": DIAGNOSIS_SCHEMA_VERSION,
            "status": "diagnosable" if options else "no_failure",
            "strategy_options": [item.model_dump(mode="json") for item in options],
            "unavailable_stages": ["fine_rank", "rerank", "query_understanding_gold"],
        }
        diagnosis_id = _stable_id("stage-diagnosis", body)
        result = StageDiagnosis(diagnosis_id=diagnosis_id, **body).model_dump(
            mode="json"
        )
        logger.info(
            "stage_diagnosis_completed",
            extra={
                "diagnosis_id": diagnosis_id,
                "finding_count": len(findings),
                "pipeline_run_id": run_id,
                "primary_category": primary,
                "query_count": len(query_diagnoses),
                "strategy_option_count": len(options),
            },
        )
        return result
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "stage_diagnosis_failed",
            extra={
                "error_code": "invalid_stage_evidence",
                "error_type": type(exc).__name__,
                "pipeline_run_id": run_id,
            },
        )
        raise
