"""Planner interface and deterministic branching planner for Runtime tests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import Protocol

from search_quality.evaluation.comparison import COMPARISON_EPSILON

from .contracts import (
    AgentDecision,
    AgentState,
    FinishDecision,
    RuntimeTask,
    TerminalOutcome,
    ToolAction,
    ToolObservation,
)


@dataclass(frozen=True, slots=True)
class PlannerView:
    task: RuntimeTask
    state: AgentState
    observations: tuple[ToolObservation, ...]
    steps_used: int
    tool_calls_used: int
    remaining_ms: int = 0


class Planner(Protocol):
    planner_id: str

    def decide(self, view: PlannerView) -> AgentDecision:
        """Return one structured decision without executing it."""


class FakeBranchingPlanner:
    """Pure smoke planner proving that an observation changes the next action.

    This is a Runtime fixture, not the final LLM Agent. It deliberately branches
    on the comparison evidence so state, policy, Trace and Replay can be trusted
    before a provider is introduced.
    """

    planner_id = "fake-branching-v1"

    def decide(self, view: PlannerView) -> AgentDecision:
        comparison = self._latest(view.observations, "compare_runs")
        if comparison is None:
            return ToolAction(
                tool_name="compare_runs",
                arguments={
                    "baseline_run_id": view.task.baseline_run_id,
                    "candidate_run_id": view.task.candidate_run_id,
                },
                reason_code="compare_requested_runs",
            )
        if comparison.status == "failed":
            return FinishDecision(
                outcome=TerminalOutcome.INCONCLUSIVE,
                evidence_refs=self._evidence(view.observations),
                reason_code="comparison_failed",
            )

        try:
            metrics, regressions, comparison_epsilon = self._validated_comparison(
                comparison.payload,
                baseline_run_id=view.task.baseline_run_id,
                candidate_run_id=view.task.candidate_run_id,
            )
            primary_delta = self._required_delta(metrics, view.task.primary_metric)
            all_metric_deltas = tuple(
                self._required_delta(metrics, name)
                for name in (
                    "ndcg@5",
                    "ndcg@10",
                    "mrr@10",
                    "success@1",
                    "success@5",
                )
            )
        except (KeyError, TypeError, ValueError):
            return FinishDecision(
                outcome=TerminalOutcome.INCONCLUSIVE,
                evidence_refs=self._evidence(view.observations),
                reason_code="comparison_evidence_invalid",
            )

        inspect_attempts = tuple(
            item for item in view.observations if item.tool_name == "inspect_query"
        )
        inspection_limit = min(
            view.task.max_regressions_to_inspect,
            len(regressions),
        )
        if len(inspect_attempts) < inspection_limit:
            target = regressions[len(inspect_attempts)]
            return ToolAction(
                tool_name="inspect_query",
                arguments={
                    "run_id": view.task.candidate_run_id,
                    "query_id": target["query_id"],
                },
                reason_code="inspect_largest_regression",
            )

        evidence_refs = self._evidence(view.observations)
        if primary_delta < -comparison_epsilon:
            return FinishDecision(
                outcome=TerminalOutcome.REJECT,
                evidence_refs=evidence_refs,
                reason_code="primary_metric_regressed",
            )
        if regressions or any(
            delta < -comparison_epsilon for delta in all_metric_deltas
        ):
            reason_code = (
                "regression_diagnosis_incomplete"
                if any(item.status == "failed" for item in inspect_attempts)
                else "mixed_metric_or_query_evidence"
            )
            return FinishDecision(
                outcome=TerminalOutcome.INCONCLUSIVE,
                evidence_refs=evidence_refs,
                reason_code=reason_code,
            )
        if primary_delta <= comparison_epsilon:
            return FinishDecision(
                outcome=TerminalOutcome.INCONCLUSIVE,
                evidence_refs=evidence_refs,
                reason_code="primary_metric_tied",
            )
        return FinishDecision(
            outcome=TerminalOutcome.ACCEPT,
            evidence_refs=evidence_refs,
            reason_code="metrics_improved_without_observed_regression",
        )

    @staticmethod
    def _latest(
        observations: tuple[ToolObservation, ...], tool_name: str
    ) -> ToolObservation | None:
        return next(
            (item for item in reversed(observations) if item.tool_name == tool_name),
            None,
        )

    @staticmethod
    def _evidence(observations: tuple[ToolObservation, ...]) -> list[str]:
        return [
            item.evidence_ref
            for item in observations
            if item.status == "succeeded" and item.evidence_ref is not None
        ]

    @staticmethod
    def _required_delta(metrics: dict[str, object], name: str) -> float:
        values = metrics[name]
        if not isinstance(values, dict) or set(values) != {
            "baseline",
            "candidate",
            "delta",
        }:
            raise ValueError("metric evidence is malformed")
        value = values["delta"]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError("metric delta must be numeric")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("metric delta must be finite")
        return result

    @staticmethod
    def _validated_comparison(
        payload: dict[str, object],
        *,
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> tuple[dict[str, object], list[dict[str, object]], float]:
        if payload["baseline_run_id"] != baseline_run_id or (
            payload["candidate_run_id"] != candidate_run_id
        ):
            raise ValueError("comparison Run pair does not match the task")
        metrics = payload["aggregate_metrics"]
        if not isinstance(metrics, dict):
            raise TypeError("aggregate metrics must be an object")
        raw_epsilon = payload["comparison_epsilon"]
        if isinstance(raw_epsilon, bool) or not isinstance(raw_epsilon, Real):
            raise TypeError("comparison epsilon must be numeric")
        comparison_epsilon = float(raw_epsilon)
        if (
            not math.isfinite(comparison_epsilon)
            or comparison_epsilon != COMPARISON_EPSILON
        ):
            raise ValueError("comparison epsilon does not match comparator policy")
        raw_regressions = payload["regressions"]
        if not isinstance(raw_regressions, list):
            raise TypeError("regressions must be a list")
        outcome_counts = payload["outcome_counts"]
        ndcg_counts = (
            outcome_counts.get("ndcg@10") if isinstance(outcome_counts, dict) else None
        )
        regressed_count = (
            ndcg_counts.get("regressed") if isinstance(ndcg_counts, dict) else None
        )
        if (
            isinstance(regressed_count, bool)
            or not isinstance(regressed_count, int)
            or regressed_count < 0
            or len(raw_regressions) != min(regressed_count, 5)
        ):
            raise ValueError("regression summary does not match outcome counts")
        for metric_name in (
            "ndcg@5",
            "ndcg@10",
            "mrr@10",
            "success@1",
            "success@5",
        ):
            delta = FakeBranchingPlanner._required_delta(metrics, metric_name)
            counts = (
                outcome_counts.get(metric_name)
                if isinstance(outcome_counts, dict)
                else None
            )
            if not isinstance(counts, dict):
                raise TypeError("outcome counts must be an object")
            improved = counts.get("improved")
            regressed = counts.get("regressed")
            if (
                isinstance(improved, bool)
                or not isinstance(improved, int)
                or improved < 0
                or isinstance(regressed, bool)
                or not isinstance(regressed, int)
                or regressed < 0
            ):
                raise ValueError("outcome counts are invalid")
            if delta > comparison_epsilon and improved == 0:
                raise ValueError("positive aggregate has no improved Query")
            if delta < -comparison_epsilon and regressed == 0:
                raise ValueError("negative aggregate has no regressed Query")
            if improved == 0 and regressed == 0 and abs(delta) > comparison_epsilon:
                raise ValueError("all-tied Queries have a non-tied aggregate")
        regressions: list[dict[str, object]] = []
        seen_query_ids: set[int] = set()
        for item in raw_regressions:
            if not isinstance(item, dict):
                raise TypeError("regression evidence must be an object")
            query_id = item.get("query_id")
            delta = item.get("ndcg@10_delta")
            if (
                isinstance(query_id, bool)
                or not isinstance(query_id, int)
                or query_id < 1
                or query_id in seen_query_ids
            ):
                raise ValueError("regression Query ID is invalid or duplicated")
            if isinstance(delta, bool) or not isinstance(delta, Real):
                raise TypeError("regression delta must be numeric")
            if not math.isfinite(float(delta)) or float(delta) >= 0.0:
                raise ValueError("regression delta must be finite and negative")
            seen_query_ids.add(query_id)
            regressions.append(item)
        return metrics, regressions, comparison_epsilon
