"""Pure task-scope and evidence-grounding rules shared by Runtime and Replay."""

from __future__ import annotations

import math
from collections.abc import Iterable
from numbers import Real
from typing import Any

from search_quality.evaluation.comparison import COMPARISON_EPSILON

from .contracts import (
    AgentTask,
    FinishDecision,
    TerminalOutcome,
    ToolAction,
    ToolObservation,
    validate_evidence_ref,
)
from .errors import AgentPolicyError


def validate_action_scope(task: AgentTask, action: ToolAction) -> None:
    """Keep every identifier-bearing action inside the current comparison task."""

    arguments = action.arguments
    if action.tool_name == "compare_runs":
        if arguments.get("baseline_run_id") != task.baseline_run_id or (
            arguments.get("candidate_run_id") != task.candidate_run_id
        ):
            raise AgentPolicyError("comparison_outside_task_scope")
    elif action.tool_name in {"evaluate_run", "inspect_query"}:
        if arguments.get("run_id") not in {
            task.baseline_run_id,
            task.candidate_run_id,
        }:
            raise AgentPolicyError("run_outside_task_scope")


def validate_finish_grounding(
    task: AgentTask,
    decision: FinishDecision,
    observations: Iterable[ToolObservation],
) -> None:
    """Require cited evidence to exist, match the task, and support direction."""

    observed_items = tuple(observations)
    if len(decision.evidence_refs) != len(set(decision.evidence_refs)):
        raise AgentPolicyError("duplicate_evidence_reference")
    successful_by_ref: dict[str, ToolObservation] = {}
    for item in observed_items:
        if item.status == "succeeded" and item.evidence_ref is not None:
            successful_by_ref[item.evidence_ref] = item
    for reference in decision.evidence_refs:
        validate_evidence_ref(reference)
        if reference not in successful_by_ref:
            raise AgentPolicyError("unknown_evidence_reference")

    cited = tuple(successful_by_ref[ref] for ref in decision.evidence_refs)
    comparisons: list[ToolObservation] = []
    for item in cited:
        _validate_observation_scope(task, item)
        if item.tool_name == "compare_runs":
            comparisons.append(item)

    if decision.outcome in {TerminalOutcome.ACCEPT, TerminalOutcome.REJECT}:
        if not comparisons:
            raise AgentPolicyError("decision_requires_comparison_evidence")
        deltas = tuple(
            (
                _primary_delta(task, item.payload),
                _comparison_epsilon(item.payload),
            )
            for item in comparisons
        )
        if decision.outcome == TerminalOutcome.ACCEPT:
            if not all(delta > epsilon for delta, epsilon in deltas):
                raise AgentPolicyError("accept_requires_positive_primary_delta")
            if not all(_supports_accept(task, item.payload) for item in comparisons):
                raise AgentPolicyError("accept_requires_unmixed_evidence")
        if decision.outcome == TerminalOutcome.REJECT and not all(
            delta < -epsilon for delta, epsilon in deltas
        ):
            raise AgentPolicyError("reject_requires_negative_primary_delta")


def _validate_observation_scope(task: AgentTask, item: ToolObservation) -> None:
    payload = item.payload
    if item.tool_name == "compare_runs":
        if payload.get("baseline_run_id") != task.baseline_run_id or (
            payload.get("candidate_run_id") != task.candidate_run_id
        ):
            raise AgentPolicyError("comparison_evidence_outside_task_scope")
        _validate_comparison_consistency(payload)
        return
    if item.tool_name in {"run_ranker", "evaluate_run", "inspect_query"}:
        if payload.get("run_id") not in {
            task.baseline_run_id,
            task.candidate_run_id,
        }:
            raise AgentPolicyError("run_evidence_outside_task_scope")


def _primary_delta(task: AgentTask, payload: dict[str, Any]) -> float:
    metrics = payload.get("aggregate_metrics")
    values = metrics.get(task.primary_metric) if isinstance(metrics, dict) else None
    value = values.get("delta") if isinstance(values, dict) else None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AgentPolicyError("comparison_primary_metric_invalid")
    result = float(value)
    if not math.isfinite(result):
        raise AgentPolicyError("comparison_primary_metric_invalid")
    return result


def _supports_accept(task: AgentTask, payload: dict[str, Any]) -> bool:
    epsilon = _comparison_epsilon(payload)
    if _primary_delta(task, payload) <= epsilon:
        return False
    regressions = payload.get("regressions")
    if not isinstance(regressions, list) or regressions:
        return False
    metrics = payload.get("aggregate_metrics")
    if not isinstance(metrics, dict):
        return False
    for name in ("ndcg@5", "ndcg@10", "mrr@10", "success@1", "success@5"):
        values = metrics.get(name)
        value = values.get("delta") if isinstance(values, dict) else None
        if isinstance(value, bool) or not isinstance(value, Real):
            return False
        delta = float(value)
        if not math.isfinite(delta) or delta < -epsilon:
            return False
    return True


def _comparison_epsilon(payload: dict[str, Any]) -> float:
    value = payload.get("comparison_epsilon")
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AgentPolicyError("comparison_epsilon_invalid")
    result = float(value)
    if not math.isfinite(result) or result != COMPARISON_EPSILON:
        raise AgentPolicyError("comparison_epsilon_invalid")
    return result


def _validate_comparison_consistency(payload: dict[str, Any]) -> None:
    epsilon = _comparison_epsilon(payload)
    metrics = payload.get("aggregate_metrics")
    outcomes = payload.get("outcome_counts")
    if not isinstance(metrics, dict) or not isinstance(outcomes, dict):
        raise AgentPolicyError("comparison_summary_invalid")
    for name in ("ndcg@5", "ndcg@10", "mrr@10", "success@1", "success@5"):
        values = metrics.get(name)
        delta_value = values.get("delta") if isinstance(values, dict) else None
        counts = outcomes.get(name)
        improved = counts.get("improved") if isinstance(counts, dict) else None
        regressed = counts.get("regressed") if isinstance(counts, dict) else None
        if (
            isinstance(delta_value, bool)
            or not isinstance(delta_value, Real)
            or not math.isfinite(float(delta_value))
            or isinstance(improved, bool)
            or not isinstance(improved, int)
            or improved < 0
            or isinstance(regressed, bool)
            or not isinstance(regressed, int)
            or regressed < 0
        ):
            raise AgentPolicyError("comparison_summary_invalid")
        delta = float(delta_value)
        if delta > epsilon and improved == 0:
            raise AgentPolicyError("comparison_summary_inconsistent")
        if delta < -epsilon and regressed == 0:
            raise AgentPolicyError("comparison_summary_inconsistent")
        if improved == 0 and regressed == 0 and abs(delta) > epsilon:
            raise AgentPolicyError("comparison_summary_inconsistent")
