"""Deterministic terminal reports built only from observed evidence."""

from __future__ import annotations

from typing import Any

from .contracts import (
    RetrievalOptimizationTask,
    RuntimeTask,
    TerminalOutcome,
    ToolObservation,
)


def build_terminal_report(
    *,
    task: RuntimeTask,
    outcome: TerminalOutcome,
    reason_code: str,
    observations: tuple[ToolObservation, ...],
    evidence_refs: list[str],
) -> dict[str, Any]:
    if isinstance(task, RetrievalOptimizationTask):
        return _build_retrieval_terminal_report(
            task=task,
            outcome=outcome,
            reason_code=reason_code,
            observations=observations,
            evidence_refs=evidence_refs,
        )

    comparison = next(
        (
            item.payload
            for item in reversed(observations)
            if item.tool_name == "compare_runs" and item.status == "succeeded"
        ),
        None,
    )
    inspected_queries = [
        {
            "evidence_ref": item.evidence_ref,
            "query_id": item.payload.get("query_id"),
            "run_id": item.payload.get("run_id"),
        }
        for item in observations
        if item.tool_name == "inspect_query" and item.status == "succeeded"
    ]
    failed_tools = [
        {"error_code": item.error_code, "tool_name": item.tool_name}
        for item in observations
        if item.status == "failed"
    ]
    return {
        "boundary": {
            "full_catalog_recall_claimed": False,
            "profile": "smoke",
            "task": "judged-candidate-reranking",
        },
        "comparison": comparison,
        "decision": {
            "outcome": outcome.value,
            "reason_code": reason_code,
        },
        "evidence_refs": evidence_refs,
        "failed_tools": failed_tools,
        "inspected_queries": inspected_queries,
        "task": task.model_dump(mode="json"),
    }


def _build_retrieval_terminal_report(
    *,
    task: RetrievalOptimizationTask,
    outcome: TerminalOutcome,
    reason_code: str,
    observations: tuple[ToolObservation, ...],
    evidence_refs: list[str],
) -> dict[str, Any]:
    baseline = next(
        (
            item.payload
            for item in observations
            if item.tool_name == "diagnose_baseline_retrieval"
            and item.status == "succeeded"
        ),
        None,
    )
    experiments = [
        {
            "candidate_run_id": item.payload.get("candidate_run_id"),
            "comparison_id": item.payload.get("comparison_id"),
            "failed_gates": item.payload.get("gate", {}).get("failed_gates", []),
            "gate_passed": item.payload.get("gate", {}).get("passed"),
            "pipeline_id": item.payload.get("pipeline_id"),
            "pipeline_variant": item.payload.get("pipeline_variant"),
        }
        for item in observations
        if item.tool_name == "run_retrieval_candidate" and item.status == "succeeded"
    ]
    failed_tools = [
        {"error_code": item.error_code, "tool_name": item.tool_name}
        for item in observations
        if item.status == "failed"
    ]
    selected_by_reason = {
        "uniform_candidate_passed": task.candidate_variants[0],
        "conservative_candidate_selected": task.candidate_variants[1],
        "aggressive_candidate_selected": task.candidate_variants[2],
        "llm_uniform_candidate_selected": task.candidate_variants[0],
        "llm_conservative_candidate_selected": task.candidate_variants[1],
        "llm_aggressive_candidate_selected": task.candidate_variants[2],
    }
    selected_variant = selected_by_reason.get(reason_code)
    selected = next(
        (item for item in experiments if item["pipeline_variant"] == selected_variant),
        None,
    )
    return {
        "boundary": {
            "denominator_complete": True,
            "full_catalog_recall_claimed": False,
            "profile": task.profile,
            "task": "query-scoped-judged-pool-candidate-retention",
            "unjudged_products_are_irrelevant": False,
        },
        "decision": {
            "outcome": outcome.value,
            "reason_code": reason_code,
            "selected_comparison_id": (
                selected["comparison_id"] if selected is not None else None
            ),
            "selected_pipeline_variant": selected_variant,
        },
        "diagnosis": (
            {
                "diagnosis_id": baseline.get("diagnosis_id"),
                "primary_category": baseline.get("primary_category"),
                "recommended_next_action": baseline.get("recommended_next_action"),
                "run_id": baseline.get("run_id"),
            }
            if baseline is not None
            else None
        ),
        "evidence_refs": evidence_refs,
        "experiments": experiments,
        "failed_tools": failed_tools,
        "task": task.model_dump(mode="json"),
    }
