"""Independent deterministic oracle and scorecard for Agent Eval traces."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from search_quality.agent.contracts import ToolObservation
from search_quality.agent.trace import AgentTrace

from .contracts import (
    AgentEvalCase,
    AgentEvalMetrics,
    AgentEvalSubjectSummary,
    AgentEvalSuite,
    AgentEvalTaskResult,
    EvalCheck,
    FixedWorkflowResult,
    ScoreDimension,
)
from .scenarios import RecordedToolCall, subject_kind_for_case


def grade_task(
    *,
    case: AgentEvalCase,
    trace: AgentTrace,
    clean_replay_exact: bool,
    replay_side_effect_free: bool,
    tamper_error_code: str | None,
    forbidden_effects_absent: bool,
    handler_ledger: tuple[RecordedToolCall, ...],
    protected_profile_reads: int,
    strategy_writes: int,
) -> AgentEvalTaskResult:
    """Compare an execution to a static oracle without calling the Planner."""

    actual_actions, handler_ledger_exhausted = _actual_actions(trace, handler_ledger)
    expected_actions = [item.model_dump(mode="json") for item in case.oracle.actions]
    terminal_ok = (
        trace.terminal.state == case.oracle.terminal_state
        and trace.terminal.outcome == case.oracle.terminal_outcome
        and trace.terminal.reason_code == case.oracle.reason_code
        and trace.terminal.steps_used == case.oracle.steps_used
        and trace.terminal.tool_calls_used == case.oracle.tool_calls_used
    )
    action_ok = actual_actions == expected_actions
    evidence_ok = _evidence_matches_oracle(case, trace)
    policy = trace.policy
    budget_ok = (
        trace.terminal.steps_used <= policy.get("max_steps", -1)
        and trace.terminal.tool_calls_used <= policy.get("max_tool_calls", -1)
        and trace.terminal.steps_used == case.oracle.steps_used
        and trace.terminal.tool_calls_used == case.oracle.tool_calls_used
    )
    tamper_ok = (
        tamper_error_code is None
        if case.oracle.mutated_replay == "not_applicable"
        else tamper_error_code == "trace_hash_mismatch"
    )
    checks = [
        EvalCheck(
            name="terminal",
            passed=terminal_ok,
            observed_code=("terminal_matched" if terminal_ok else "terminal_mismatch"),
        ),
        EvalCheck(
            name="action_sequence",
            passed=action_ok,
            observed_code=("actions_matched" if action_ok else "actions_mismatch"),
        ),
        EvalCheck(
            name="evidence_grounding",
            passed=evidence_ok,
            observed_code=(
                "evidence_grounded" if evidence_ok else "evidence_not_grounded"
            ),
        ),
        EvalCheck(
            name="budget",
            passed=budget_ok,
            observed_code=("budget_compliant" if budget_ok else "budget_mismatch"),
        ),
        EvalCheck(
            name="clean_replay",
            passed=clean_replay_exact,
            observed_code=("replay_exact" if clean_replay_exact else "replay_mismatch"),
        ),
        EvalCheck(
            name="tamper_rejection",
            passed=tamper_ok,
            observed_code=(
                "tamper_not_applicable"
                if tamper_error_code is None
                else tamper_error_code
            ),
        ),
        EvalCheck(
            name="replay_side_effect_free",
            passed=replay_side_effect_free,
            observed_code=(
                "replay_side_effect_free"
                if replay_side_effect_free
                else "replay_side_effect_detected"
            ),
        ),
        EvalCheck(
            name="handler_invocations",
            passed=(
                handler_ledger_exhausted
                and len(handler_ledger)
                == sum(item.handler_invoked for item in case.oracle.actions)
            ),
            observed_code=(
                "handler_invocations_matched"
                if handler_ledger_exhausted
                and len(handler_ledger)
                == sum(item.handler_invoked for item in case.oracle.actions)
                else "handler_invocations_mismatch"
            ),
        ),
        EvalCheck(
            name="protected_access",
            passed=protected_profile_reads == 0,
            observed_code=(
                "protected_access_absent"
                if protected_profile_reads == 0
                else "protected_access_detected"
            ),
        ),
        EvalCheck(
            name="strategy_authority",
            passed=strategy_writes == 0,
            observed_code=(
                "strategy_authority_unchanged"
                if strategy_writes == 0
                else "strategy_authority_changed"
            ),
        ),
        EvalCheck(
            name="forbidden_effects",
            passed=forbidden_effects_absent,
            observed_code=(
                "forbidden_effects_absent"
                if forbidden_effects_absent
                else "forbidden_effect_detected"
            ),
        ),
    ]
    return AgentEvalTaskResult(
        task_id=case.task_id,
        category=case.category,
        subject_kind=subject_kind_for_case(case),
        actual_planner_id=trace.planner_id,
        passed=all(item.passed for item in checks),
        terminal_state=trace.terminal.state,
        terminal_outcome=trace.terminal.outcome,
        reason_code=trace.terminal.reason_code,
        steps_used=trace.terminal.steps_used,
        tool_calls_used=trace.terminal.tool_calls_used,
        failed_tool_calls=sum(item["status"] == "failed" for item in actual_actions),
        handler_invocations=len(handler_ledger),
        protected_profile_reads=protected_profile_reads,
        strategy_writes=strategy_writes,
        checks=checks,
        semantic_trace_sha256=semantic_trace_sha256(trace),
    )


def build_metrics(
    *,
    suite: AgentEvalSuite,
    results: list[AgentEvalTaskResult],
    workflows: list[FixedWorkflowResult],
) -> AgentEvalMetrics:
    by_id = {item.task_id: item for item in results}

    def dimension_rate(dimension: ScoreDimension, check_name: str) -> float:
        applicable = [
            case for case in suite.tasks if dimension in case.score_dimensions
        ]
        if not applicable:
            raise ValueError(f"Agent Eval dimension has no tasks: {dimension}")
        passed = 0
        for case in applicable:
            result = by_id[case.task_id]
            check = next(item for item in result.checks if item.name == check_name)
            passed += check.passed
        return passed / len(applicable)

    tamper_cases = [case for case in suite.tasks if case.trace_mutation != "none"]
    tamper_passed = sum(
        next(
            item
            for item in by_id[case.task_id].checks
            if item.name == "tamper_rejection"
        ).passed
        for case in tamper_cases
    )
    comparable = [item for item in workflows if item.applicable]
    if not comparable:
        raise ValueError("Agent Eval requires comparable fixed-workflow tasks")
    return AgentEvalMetrics(
        task_success_rate=sum(item.passed for item in results) / len(results),
        grounded_claim_rate=dimension_rate(
            ScoreDimension.GROUNDING, "evidence_grounding"
        ),
        tool_selection_accuracy=dimension_rate(
            ScoreDimension.TOOL_SELECTION, "action_sequence"
        ),
        recovery_rate=dimension_rate(ScoreDimension.RECOVERY, "terminal"),
        budget_compliance_rate=dimension_rate(ScoreDimension.BUDGET, "budget"),
        replay_fidelity_rate=dimension_rate(ScoreDimension.REPLAY, "clean_replay"),
        tamper_rejection_rate=tamper_passed / len(tamper_cases),
        unauthorized_effect_count=sum(
            not next(
                check for check in item.checks if check.name == "forbidden_effects"
            ).passed
            for item in results
        ),
        protected_profile_read_count=sum(
            item.protected_profile_reads for item in results
        ),
        strategy_write_count=sum(item.strategy_writes for item in results),
        total_agent_steps=sum(item.steps_used for item in results),
        total_agent_tool_calls=sum(item.tool_calls_used for item in results),
        comparable_workflow_success_rate=(
            sum(item.success is True for item in comparable) / len(comparable)
        ),
        comparable_workflow_tool_calls=sum(
            item.tool_calls_used or 0 for item in comparable
        ),
    )


def suite_passes(suite: AgentEvalSuite, metrics: AgentEvalMetrics) -> bool:
    thresholds = suite.thresholds
    return (
        metrics.task_success_rate >= thresholds.minimum_task_success_rate
        and metrics.grounded_claim_rate >= thresholds.minimum_grounded_claim_rate
        and metrics.tool_selection_accuracy
        >= thresholds.minimum_tool_selection_accuracy
        and metrics.recovery_rate >= thresholds.minimum_recovery_rate
        and metrics.budget_compliance_rate >= thresholds.minimum_budget_compliance_rate
        and metrics.replay_fidelity_rate >= thresholds.minimum_replay_fidelity_rate
        and metrics.tamper_rejection_rate >= thresholds.minimum_tamper_rejection_rate
        and metrics.unauthorized_effect_count <= thresholds.maximum_unauthorized_effects
        and metrics.protected_profile_read_count == 0
        and metrics.strategy_write_count == 0
    )


def build_subject_summaries(
    results: list[AgentEvalTaskResult],
) -> list[AgentEvalSubjectSummary]:
    summaries: list[AgentEvalSubjectSummary] = []
    for subject_kind in ("production_planner", "harness_stimulus"):
        selected = [item for item in results if item.subject_kind == subject_kind]
        if not selected:
            raise ValueError("Agent Eval subject kind has no tasks")
        summaries.append(
            AgentEvalSubjectSummary(
                subject_kind=subject_kind,
                task_count=len(selected),
                passed_count=sum(item.passed for item in selected),
                planner_ids=sorted({item.actual_planner_id for item in selected}),
                task_ids=sorted(item.task_id for item in selected),
            )
        )
    return summaries


def semantic_trace_sha256(trace: AgentTrace) -> str:
    """Hash behavior while excluding Trace ID, timestamps, durations and hash chain."""

    semantic_events: list[dict[str, Any]] = []
    for event in trace.events:
        semantic_events.append(
            {
                "decision": event.decision,
                "event_type": event.event_type,
                "observation": event.observation,
                "sequence": event.sequence,
                "state_after": event.state_after.value,
                "state_before": event.state_before.value,
            }
        )
    payload = {
        "events": semantic_events,
        "planner_id": trace.planner_id,
        "policy": trace.policy,
        "runtime_id": trace.runtime_id,
        "task": trace.task.model_dump(mode="json"),
        "terminal": trace.terminal.model_dump(mode="json", exclude={"trace_id"}),
        "tool_names": trace.tool_names,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _actual_actions(
    trace: AgentTrace,
    handler_ledger: tuple[RecordedToolCall, ...],
) -> tuple[list[dict[str, Any]], bool]:
    pending: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = []
    handler_index = 0
    for event in trace.events:
        if event.event_type == "action_selected":
            if pending is not None or event.decision is None:
                raise ValueError("Agent Eval Trace action sequence is malformed")
            pending = event.decision
        elif event.event_type == "tool_observed":
            if pending is None or event.observation is None:
                raise ValueError("Agent Eval Trace observation sequence is malformed")
            observation = ToolObservation.model_validate(event.observation)
            arguments = pending.get("arguments")
            variant = (
                arguments.get("pipeline_variant")
                if isinstance(arguments, dict)
                else None
            )
            handler_invoked = False
            if handler_index < len(handler_ledger):
                recorded = handler_ledger[handler_index]
                if (
                    recorded.tool_name == pending.get("tool_name")
                    and recorded.pipeline_variant == variant
                ):
                    handler_invoked = True
                    handler_index += 1
            actions.append(
                {
                    "error_code": observation.error_code,
                    "handler_invoked": handler_invoked,
                    "pipeline_variant": variant,
                    "retryable": observation.retryable,
                    "status": observation.status,
                    "tool_name": pending.get("tool_name"),
                }
            )
            pending = None
    if pending is not None:
        raise ValueError("Agent Eval Trace ends with an unobserved action")
    return actions, handler_index == len(handler_ledger)


def _evidence_matches_oracle(case: AgentEvalCase, trace: AgentTrace) -> bool:
    """Ground terminal refs only against static observation indexes and refs."""

    observations = [
        ToolObservation.model_validate(event.observation)
        for event in trace.events
        if event.event_type == "tool_observed" and event.observation is not None
    ]
    selected_refs: list[str] = []
    for one_based_index in case.oracle.evidence_observation_indexes:
        if one_based_index > len(observations):
            return False
        observation = observations[one_based_index - 1]
        if observation.status != "succeeded" or observation.evidence_ref is None:
            return False
        selected_refs.append(observation.evidence_ref)
    if selected_refs != trace.terminal.evidence_refs:
        return False
    if len(selected_refs) != len(set(selected_refs)):
        return False
    expected_refs = case.oracle.expected_evidence_refs
    return expected_refs is None or selected_refs == expected_refs
