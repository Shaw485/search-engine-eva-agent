"""Select bounded, truthful Query examples for the protected Agent workbench."""

from __future__ import annotations

import copy
from typing import Any

CHANGE_EPSILON = 1e-12
DEFAULT_EXAMPLE_LIMIT = 10


def select_changed_query_examples(
    experiments: list[dict[str, Any]],
    *,
    selected_candidate_run_id: str,
    limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Return changed Query examples while preserving both outcome directions.

    The release gates still use each comparison's complete ``per_query`` list.
    This selector only creates a bounded display projection.  A Query may appear
    once per outcome direction when different ablation candidates improve and
    regress it; the candidate variant is included so that evidence is not mixed.
    """

    safe_limit = max(0, min(int(limit), DEFAULT_EXAMPLE_LIMIT))
    if safe_limit == 0:
        return []

    best_by_query_and_outcome: dict[tuple[str, int, str], dict[str, Any]] = {}
    for experiment in experiments:
        candidate = experiment["candidate"]
        comparison = experiment["comparison"]
        candidate_run_id = str(candidate["run_id"])
        pipeline_variant = str(candidate["pipeline"]["variant"])
        comparison_id = str(comparison["comparison_id"])
        is_selected_comparison = candidate_run_id == selected_candidate_run_id
        gate_passed = comparison["gate_result"]["passed"] is True
        for query in comparison["per_query"]:
            delta = float(query["coarse_ndcg@10_delta"])
            if delta > CHANGE_EPSILON:
                outcome = "improvement"
            elif delta < -CHANGE_EPSILON:
                outcome = "regression"
            else:
                continue
            example = {
                "baseline_top_results": copy.deepcopy(query["baseline_top_results"]),
                "candidate_run_id": candidate_run_id,
                "candidate_top_results": copy.deepcopy(query["candidate_top_results"]),
                "coarse_ndcg@10_delta": delta,
                "comparison_id": comparison_id,
                "fusion_ndcg@10_delta": float(query["fusion_ndcg@10_delta"]),
                "gate_passed": gate_passed,
                "is_selected_comparison": is_selected_comparison,
                "locale": str(query["locale"]),
                "outcome": outcome,
                "pipeline_variant": pipeline_variant,
                "query_id": int(query["query_id"]),
                "query_text": str(query["query_text"]),
                "recovered_relevant": copy.deepcopy(query["recovered_relevant"]),
                "union_coverage_delta": float(query["union_coverage_delta"]),
            }
            key = (str(query["locale"]), int(query["query_id"]), outcome)
            current = best_by_query_and_outcome.get(key)
            if current is None or _same_query_preference(
                example
            ) < _same_query_preference(current):
                best_by_query_and_outcome[key] = example

    changed = sorted(best_by_query_and_outcome.values(), key=_display_order)
    selected = changed[:safe_limit]
    if safe_limit >= 2 and selected:
        available_outcomes = {item["outcome"] for item in changed}
        selected_outcomes = {item["outcome"] for item in selected}
        for required_outcome in ("improvement", "regression"):
            if (
                required_outcome in available_outcomes
                and required_outcome not in selected_outcomes
            ):
                selected[-1] = next(
                    item for item in changed if item["outcome"] == required_outcome
                )
                selected_outcomes.add(required_outcome)
    return sorted(selected, key=_display_order)


def _same_query_preference(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        not bool(item["is_selected_comparison"]),
        not bool(item["gate_passed"]),
        -abs(float(item["coarse_ndcg@10_delta"])),
        str(item["pipeline_variant"]),
        str(item["comparison_id"]),
        str(item["candidate_run_id"]),
    )


def _display_order(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -abs(float(item["coarse_ndcg@10_delta"])),
        0 if item["outcome"] == "improvement" else 1,
        str(item["locale"]),
        int(item["query_id"]),
        str(item["pipeline_variant"]),
    )
