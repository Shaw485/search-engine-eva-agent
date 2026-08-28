"""Observation-driven deterministic planner for retrieval-stage optimization."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    RETRIEVAL_PIPELINE_VARIANTS,
    AgentDecision,
    FinishDecision,
    RetrievalOptimizationTask,
    RetrievalPipelineVariant,
    TerminalOutcome,
    ToolAction,
    ToolObservation,
)
from .planner import PlannerView
from .retrieval_tools import (
    DIAGNOSE_BASELINE_TOOL,
    RUN_CANDIDATE_TOOL,
    BaselineDiagnosisOutput,
    CandidateExperimentOutput,
    CandidateExperimentPayload,
)

UNIFORM_VARIANT = RETRIEVAL_PIPELINE_VARIANTS[0]
CONSERVATIVE_VARIANT = RETRIEVAL_PIPELINE_VARIANTS[1]
AGGRESSIVE_VARIANT = RETRIEVAL_PIPELINE_VARIANTS[2]


@dataclass(frozen=True, slots=True)
class _History:
    decision: AgentDecision
    experiments: tuple[CandidateExperimentPayload, ...]


class ObservationDrivenRetrievalPlanner:
    """Run only the fixed candidate sequence justified by observed gates."""

    planner_id = "stage-aware-retrieval-planner-v1"

    def decide(self, view: PlannerView) -> AgentDecision:
        if not isinstance(view.task, RetrievalOptimizationTask):
            raise TypeError("retrieval planner requires a retrieval optimization task")
        return expected_retrieval_decision(view.task, view.observations)


def expected_retrieval_decision(
    task: RetrievalOptimizationTask,
    observations: tuple[ToolObservation, ...],
) -> AgentDecision:
    """Recompute the one valid next action from ordered observations."""

    if task.candidate_variants != RETRIEVAL_PIPELINE_VARIANTS:
        raise ValueError("retrieval candidate space is not trusted")
    return _analyze_history(task, observations).decision


def validate_retrieval_plan_semantics(
    task: RetrievalOptimizationTask,
    observations: tuple[ToolObservation, ...],
    terminal: FinishDecision,
) -> None:
    """Validate a terminal path without executing tools.

    Replay uses this to reject reordered/skipped candidates, inconsistent gate
    summaries, excessive retry, and a selected candidate that did not pass gates.
    """

    expected = expected_retrieval_decision(task, observations)
    if not isinstance(expected, FinishDecision):
        raise ValueError("retrieval trace terminated before the required next action")
    if terminal != expected:
        raise ValueError("retrieval terminal decision does not match observations")


def _analyze_history(
    task: RetrievalOptimizationTask,
    observations: tuple[ToolObservation, ...],
) -> _History:
    expected_tool = DIAGNOSE_BASELINE_TOOL
    expected_variant: RetrievalPipelineVariant | None = None
    baseline_run_id: str | None = None
    experiments: list[CandidateExperimentPayload] = []
    retry_consumed = False

    for observation in observations:
        if observation.tool_name != expected_tool:
            raise ValueError("retrieval observation tool order is invalid")
        if observation.status == "failed":
            _validate_failed_observation(observation)
            if observation.retryable and not retry_consumed:
                retry_consumed = True
                continue
            reason = (
                "retrieval_tool_retry_exhausted"
                if observation.retryable and retry_consumed
                else "retrieval_tool_failed"
            )
            decision = FinishDecision(
                outcome=TerminalOutcome.INCONCLUSIVE,
                evidence_refs=_successful_evidence(observations, stop=observation),
                reason_code=reason,
            )
            if observation is not observations[-1]:
                raise ValueError(
                    "retrieval observations continue after terminal failure"
                )
            return _History(decision=decision, experiments=tuple(experiments))

        _validate_success_observation(observation)
        if expected_tool == DIAGNOSE_BASELINE_TOOL:
            envelope = BaselineDiagnosisOutput.model_validate(
                {
                    "evidence_ref": observation.evidence_ref,
                    "payload": observation.payload,
                },
                strict=True,
            )
            if envelope.payload.profile != task.profile:
                raise ValueError("baseline profile is outside task scope")
            baseline_run_id = envelope.payload.run_id
            expected_tool = RUN_CANDIDATE_TOOL
            expected_variant = UNIFORM_VARIANT
            continue

        if baseline_run_id is None or expected_variant is None:
            raise ValueError("candidate experiment has no trusted baseline")
        envelope = CandidateExperimentOutput.model_validate(
            {
                "evidence_ref": observation.evidence_ref,
                "payload": observation.payload,
            },
            strict=True,
        )
        experiment = envelope.payload
        if experiment.profile != task.profile:
            raise ValueError("candidate profile is outside task scope")
        if experiment.baseline_run_id != baseline_run_id:
            raise ValueError("candidate baseline does not match diagnosed baseline")
        if experiment.pipeline_variant != expected_variant:
            raise ValueError("retrieval candidate was reordered or skipped")
        if any(
            item.pipeline_variant == experiment.pipeline_variant for item in experiments
        ):
            raise ValueError("retrieval candidate was executed more than once")
        if any(
            item.candidate_run_id == experiment.candidate_run_id
            or item.diagnosis_id == experiment.diagnosis_id
            or item.comparison_id == experiment.comparison_id
            for item in experiments
        ):
            raise ValueError("retrieval candidate evidence IDs must be unique")
        experiments.append(experiment)

        if expected_variant == UNIFORM_VARIANT:
            if experiment.gate.passed:
                decision = _proposal_ready(
                    observations,
                    reason_code="uniform_candidate_passed",
                )
                if observation is not observations[-1]:
                    raise ValueError(
                        "observations continue after a passing uniform candidate"
                    )
                return _History(decision=decision, experiments=tuple(experiments))
            expected_variant = CONSERVATIVE_VARIANT
            continue
        if expected_variant == CONSERVATIVE_VARIANT:
            # Even a passing conservative candidate gets one bounded upside probe.
            expected_variant = AGGRESSIVE_VARIANT
            continue

        passing = [item for item in experiments if item.gate.passed]
        if not passing:
            decision = FinishDecision(
                outcome=TerminalOutcome.NO_SAFE_IMPROVEMENT,
                evidence_refs=_successful_evidence(observations),
                reason_code="no_safe_candidate",
            )
        else:
            selected = max(passing, key=lambda item: item.selection_key)
            reason_by_variant = {
                CONSERVATIVE_VARIANT: "conservative_candidate_selected",
                AGGRESSIVE_VARIANT: "aggressive_candidate_selected",
            }
            reason = reason_by_variant.get(selected.pipeline_variant)
            if reason is None:
                raise ValueError("selected candidate is inconsistent with bounded path")
            decision = _proposal_ready(observations, reason_code=reason)
        if observation is not observations[-1]:
            raise ValueError(
                "retrieval observations continue after terminal experiment"
            )
        return _History(decision=decision, experiments=tuple(experiments))

    if expected_tool == DIAGNOSE_BASELINE_TOOL:
        decision = ToolAction(
            tool_name=DIAGNOSE_BASELINE_TOOL,
            arguments={"profile": task.profile},
            reason_code="diagnose_retrieval_baseline",
        )
    else:
        if baseline_run_id is None or expected_variant is None:
            raise ValueError("retrieval planner history is incomplete")
        decision = ToolAction(
            tool_name=RUN_CANDIDATE_TOOL,
            arguments={
                "baseline_run_id": baseline_run_id,
                "pipeline_variant": expected_variant,
            },
            reason_code=_experiment_reason(expected_variant),
        )
    return _History(decision=decision, experiments=tuple(experiments))


def _proposal_ready(
    observations: tuple[ToolObservation, ...],
    *,
    reason_code: str,
) -> FinishDecision:
    return FinishDecision(
        outcome=TerminalOutcome.PROPOSAL_READY,
        evidence_refs=_successful_evidence(observations),
        reason_code=reason_code,
    )


def _successful_evidence(
    observations: tuple[ToolObservation, ...],
    *,
    stop: ToolObservation | None = None,
) -> list[str]:
    result: list[str] = []
    for item in observations:
        if item is stop:
            break
        if item.status == "succeeded" and item.evidence_ref is not None:
            result.append(item.evidence_ref)
    return result


def _experiment_reason(variant: RetrievalPipelineVariant) -> str:
    return {
        UNIFORM_VARIANT: "test_uniform_multifield_fusion",
        CONSERVATIVE_VARIANT: "test_conservative_multifield_fusion",
        AGGRESSIVE_VARIANT: "probe_aggressive_multifield_fusion",
    }[variant]


def _validate_success_observation(observation: ToolObservation) -> None:
    if (
        observation.evidence_ref is None
        or observation.error_code is not None
        or observation.retryable
    ):
        raise ValueError("successful retrieval observation envelope is malformed")


def _validate_failed_observation(observation: ToolObservation) -> None:
    if (
        observation.evidence_ref is not None
        or observation.payload
        or observation.error_code is None
    ):
        raise ValueError("failed retrieval observation envelope is malformed")
