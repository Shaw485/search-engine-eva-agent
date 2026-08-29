"""Trusted comparison and gates for query-scoped retrieval experiments."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .retrieval_validation import validate_retrieval_run

COMPARISON_SCHEMA_VERSION = "query-scoped-retrieval-comparison-v1"
GATE_POLICY_VERSION = "closed-retrieval-experiment-gates-v1"


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be finite and in [0, 1]")
    return result


def _metric(run: dict[str, Any], stage_id: str, metric_name: str) -> float:
    try:
        value = run["aggregate"]["stages"][stage_id][metric_name]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Run is missing {stage_id}.{metric_name}") from exc
    return _number(value, field=f"{stage_id}.{metric_name}")


def _delta(baseline: float, candidate: float) -> dict[str, float]:
    return {
        "baseline": baseline,
        "candidate": candidate,
        "delta": candidate - baseline,
    }


def _check(
    *,
    name: str,
    observed: float,
    comparator: str,
    threshold: float,
) -> dict[str, Any]:
    if not math.isfinite(observed) or not math.isfinite(threshold):
        raise ValueError("retrieval gate values must be finite")
    if comparator == ">":
        passed = observed > threshold
    elif comparator == ">=":
        passed = observed >= threshold
    elif comparator == "<=":
        passed = observed <= threshold
    else:
        raise ValueError("unsupported retrieval gate comparator")
    return {
        "comparator": comparator,
        "name": name,
        "observed": observed,
        "passed": passed,
        "threshold": threshold,
    }


def _validate_compatible_runs(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[
    dict[tuple[str, int], dict[str, Any]], dict[tuple[str, int], dict[str, Any]]
]:
    baseline_queries = validate_retrieval_run(baseline, role="baseline")
    candidate_queries = validate_retrieval_run(candidate, role="candidate")
    if baseline.get("run_id") == candidate.get("run_id"):
        raise ValueError("retrieval comparison Runs must differ")
    for field in (
        "code_revision",
        "dataset",
        "evaluation_boundary",
        "relevance_policy",
    ):
        if baseline.get(field) != candidate.get(field):
            raise ValueError(f"retrieval comparison requires matching {field}")
    if baseline.get("pipeline", {}).get("variant") != "title-exact-v1":
        raise ValueError("baseline must use the title-exact-v1 pipeline")
    if candidate.get("pipeline", {}).get("variant") not in {
        "title-exact-multifield-v1",
        "title-exact-multifield-weighted-v1",
        "title-exact-multifield-weighted-aggressive-v1",
    }:
        raise ValueError("candidate must use an allowlisted multi-field pipeline")
    if set(baseline_queries) != set(candidate_queries):
        raise ValueError("retrieval Runs must contain the same Query keys")
    for query_key in baseline_queries:
        before = baseline_queries[query_key]
        after = candidate_queries[query_key]
        for field in ("judgments", "pool_count", "query_text", "relevant_count"):
            if before.get(field) != after.get(field):
                raise ValueError(
                    "retrieval Runs must use identical Query pools and judgments"
                )
    return baseline_queries, candidate_queries


def _top_results(query: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    titles = {
        (item["locale"], item["product_id"]): item["product_title"]
        for item in query["judgments"]
    }
    return [
        {
            "label": item["label"],
            "locale": item["locale"],
            "product_id": item["product_id"],
            "product_title": titles[(item["locale"], item["product_id"])],
            "rank": item["rank"],
        }
        for item in query["rankings"]["coarse_rank"][:limit]
    ]


def _recovered_relevant(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    before_lineage = {
        (item["locale"], item["product_id"]): item for item in before["lineage"]
    }
    after_lineage = {
        (item["locale"], item["product_id"]): item for item in after["lineage"]
    }
    titles = {
        (item["locale"], item["product_id"]): item["product_title"]
        for item in after["judgments"]
    }
    recovered = []
    for key, prior in before_lineage.items():
        current = after_lineage[key]
        if (
            prior["first_loss_stage"] == "recall"
            and current["first_loss_stage"] != "recall"
        ):
            recovered.append(
                {
                    "candidate_first_loss_stage": current["first_loss_stage"],
                    "candidate_multi_field_rank": current["route_ranks"].get(
                        "multi-field-bm25-recall-v1"
                    ),
                    "label": current["label"],
                    "locale": key[0],
                    "product_id": key[1],
                    "product_title": titles[key],
                }
            )
    return sorted(
        recovered,
        key=lambda item: (
            item["candidate_multi_field_rank"] is None,
            item["candidate_multi_field_rank"] or 0,
            item["locale"],
            item["product_id"],
        ),
    )


def compare_retrieval_runs(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Compare one fixed multi-field recall experiment with its baseline."""

    baseline_queries, candidate_queries = _validate_compatible_runs(baseline, candidate)
    union = _delta(
        _metric(
            baseline,
            "recall-union-v1",
            "mean_judged_relevant_coverage",
        ),
        _metric(
            candidate,
            "recall-union-v1",
            "mean_judged_relevant_coverage",
        ),
    )
    stage_metrics: dict[str, dict[str, dict[str, float]]] = {}
    for display_stage, stage_id in (
        ("fusion", "rrf-v1"),
        ("coarse_rank", "coarse-title-bm25-v1"),
    ):
        stage_metrics[display_stage] = {
            metric_name.removeprefix("mean_"): _delta(
                _metric(baseline, stage_id, metric_name),
                _metric(candidate, stage_id, metric_name),
            )
            for metric_name in (
                "mean_judged_recall@5",
                "mean_judged_recall@10",
                "mean_mrr@10",
                "mean_ndcg@10",
            )
        }
    unique_by_channel = candidate.get("aggregate", {}).get("unique_relevant_by_channel")
    if not isinstance(unique_by_channel, dict):
        raise ValueError("candidate Run is missing per-channel unique contribution")
    unique_relevant = unique_by_channel.get("multi-field-bm25-recall-v1")
    if (
        isinstance(unique_relevant, bool)
        or not isinstance(unique_relevant, int)
        or unique_relevant < 0
    ):
        raise ValueError("multi-field unique relevant contribution is invalid")
    per_query = []
    for query_key in sorted(baseline_queries):
        before = baseline_queries[query_key]
        after = candidate_queries[query_key]
        if before["query_text"] != after["query_text"]:
            raise ValueError("retrieval Runs contain conflicting Query identity")
        before_union = before["metrics"]["recall_union"]["judged_relevant_coverage"]
        after_union = after["metrics"]["recall_union"]["judged_relevant_coverage"]
        before_ndcg = before["metrics"]["coarse_rank"]["ndcg@10"]
        after_ndcg = after["metrics"]["coarse_rank"]["ndcg@10"]
        before_fusion_ndcg = before["metrics"]["fusion"]["ndcg@10"]
        after_fusion_ndcg = after["metrics"]["fusion"]["ndcg@10"]
        per_query.append(
            {
                "baseline_top_results": _top_results(before),
                "candidate_top_results": _top_results(after),
                "coarse_ndcg@10_delta": after_ndcg - before_ndcg,
                "fusion_ndcg@10_delta": after_fusion_ndcg - before_fusion_ndcg,
                "locale": query_key[0],
                "query_id": query_key[1],
                "query_text": before["query_text"],
                "recovered_relevant": _recovered_relevant(before, after),
                "union_coverage_delta": after_union - before_union,
            }
        )
    per_query.sort(
        key=lambda item: (
            -abs(item["coarse_ndcg@10_delta"]),
            item["locale"],
            item["query_id"],
        )
    )
    worst_query_delta = min(item["coarse_ndcg@10_delta"] for item in per_query)
    regressed_query_rate = sum(
        item["coarse_ndcg@10_delta"] < -1e-12 for item in per_query
    ) / len(per_query)
    worst_fusion_query_delta = min(item["fusion_ndcg@10_delta"] for item in per_query)
    fusion_regressed_query_rate = sum(
        item["fusion_ndcg@10_delta"] < -1e-12 for item in per_query
    ) / len(per_query)
    gates = [
        _check(
            name="unique_relevant_contribution",
            observed=float(unique_relevant),
            comparator=">",
            threshold=0.0,
        ),
        _check(
            name="union_coverage_improvement",
            observed=union["delta"],
            comparator=">",
            threshold=0.0,
        ),
        _check(
            name="fusion_recall_at_10_floor",
            observed=stage_metrics["fusion"]["judged_recall@10"]["delta"],
            comparator=">=",
            threshold=0.0,
        ),
        _check(
            name="fusion_ndcg_at_10_floor",
            observed=stage_metrics["fusion"]["ndcg@10"]["delta"],
            comparator=">=",
            threshold=0.0,
        ),
        _check(
            name="fusion_mrr_at_10_floor",
            observed=stage_metrics["fusion"]["mrr@10"]["delta"],
            comparator=">=",
            threshold=0.0,
        ),
        _check(
            name="coarse_recall_at_10_floor",
            observed=stage_metrics["coarse_rank"]["judged_recall@10"]["delta"],
            comparator=">=",
            threshold=0.0,
        ),
        _check(
            name="coarse_ndcg_at_10_floor",
            observed=stage_metrics["coarse_rank"]["ndcg@10"]["delta"],
            comparator=">=",
            threshold=0.0,
        ),
        _check(
            name="coarse_mrr_at_10_floor",
            observed=stage_metrics["coarse_rank"]["mrr@10"]["delta"],
            comparator=">=",
            threshold=0.0,
        ),
        _check(
            name="worst_query_coarse_ndcg_delta_floor",
            observed=worst_query_delta,
            comparator=">=",
            threshold=-0.02,
        ),
        _check(
            name="regressed_query_rate_ceiling",
            observed=regressed_query_rate,
            comparator="<=",
            threshold=0.1,
        ),
        _check(
            name="worst_query_fusion_ndcg_delta_floor",
            observed=worst_fusion_query_delta,
            comparator=">=",
            threshold=-0.02,
        ),
        _check(
            name="fusion_regressed_query_rate_ceiling",
            observed=fusion_regressed_query_rate,
            comparator="<=",
            threshold=0.1,
        ),
    ]
    passed = all(check["passed"] for check in gates)
    if passed:
        recommendation = "review_candidate"
        next_action = "request_owner_review"
    elif union["delta"] > 0.0:
        recommendation = "reject_candidate"
        next_action = "run_recall_channel_and_rrf_ablation"
    else:
        recommendation = "reject_candidate"
        next_action = "replace_recall_candidate"
    body: dict[str, Any] = {
        "aggregate_deltas": {
            "recall_union": {
                "judged_relevant_coverage": union,
            },
            **stage_metrics,
        },
        "candidate_stage_transitions": {
            metric_name: {
                "coarse_rank": stage_metrics["coarse_rank"][metric_name]["candidate"],
                "delta": (
                    stage_metrics["coarse_rank"][metric_name]["candidate"]
                    - stage_metrics["fusion"][metric_name]["candidate"]
                ),
                "fusion": stage_metrics["fusion"][metric_name]["candidate"],
            }
            for metric_name in (
                "judged_recall@10",
                "mrr@10",
                "ndcg@10",
            )
        },
        "baseline_run_id": baseline["run_id"],
        "candidate_run_id": candidate["run_id"],
        "candidate_strategy": {
            "channel_id": "multi-field-bm25-recall-v1",
            "fields": ["title", "brand", "bullet_point", "description"],
            "fusion_weights": candidate["pipeline"]["fusion"]["weights"],
            "pipeline_variant": candidate["pipeline"]["variant"],
            "unique_relevant_contribution": unique_relevant,
        },
        "evaluation_boundary": baseline["evaluation_boundary"],
        "gate_result": {
            "checks": gates,
            "passed": passed,
            "policy_version": GATE_POLICY_VERSION,
        },
        "next_action": next_action,
        "per_query": per_query,
        "recommendation": recommendation,
        "schema_version": COMPARISON_SCHEMA_VERSION,
    }
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    body["comparison_id"] = (
        f"retrieval-comparison-{hashlib.sha256(canonical).hexdigest()[:12]}"
    )
    return body
