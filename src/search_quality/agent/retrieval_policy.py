"""Planner-independent finite policy for adaptive retrieval decisions."""

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
from .retrieval_tools import (
    DIAGNOSE_BASELINE_TOOL,
    RUN_CANDIDATE_TOOL,
    BaselineDiagnosisOutput,
    BaselineDiagnosisPayload,
    CandidateExperimentOutput,
    CandidateExperimentPayload,
)

DIAGNOSE_BASELINE_OPTION = "diagnose_baseline"
RUN_UNIFORM_OPTION = "run_uniform_candidate"
RUN_CONSERVATIVE_OPTION = "run_conservative_candidate"
RUN_AGGRESSIVE_OPTION = "run_aggressive_candidate"
FINISH_BEST_OPTION = "finish_best_passing_candidate"
FINISH_NO_SAFE_OPTION = "finish_no_safe_improvement"

OPTION_BY_VARIANT: dict[RetrievalPipelineVariant, str] = {
    RETRIEVAL_PIPELINE_VARIANTS[0]: RUN_UNIFORM_OPTION,
    RETRIEVAL_PIPELINE_VARIANTS[1]: RUN_CONSERVATIVE_OPTION,
    RETRIEVAL_PIPELINE_VARIANTS[2]: RUN_AGGRESSIVE_OPTION,
}
RUN_REASON_BY_VARIANT: dict[RetrievalPipelineVariant, str] = {
    RETRIEVAL_PIPELINE_VARIANTS[0]: "llm_test_uniform_candidate",
    RETRIEVAL_PIPELINE_VARIANTS[1]: "llm_test_conservative_candidate",
    RETRIEVAL_PIPELINE_VARIANTS[2]: "llm_test_aggressive_candidate",
}
FINISH_REASON_BY_VARIANT: dict[RetrievalPipelineVariant, str] = {
    RETRIEVAL_PIPELINE_VARIANTS[0]: "llm_uniform_candidate_selected",
    RETRIEVAL_PIPELINE_VARIANTS[1]: "llm_conservative_candidate_selected",
    RETRIEVAL_PIPELINE_VARIANTS[2]: "llm_aggressive_candidate_selected",
}


@dataclass(frozen=True, slots=True)
class RetrievalDecisionOption:
    """One server-generated option the LLM may select in the current state."""

    option_id: str
    decision: AgentDecision
    description_code: str


@dataclass(frozen=True, slots=True)
class AdaptiveRetrievalHistory:
    baseline: BaselineDiagnosisPayload | None
    experiments: tuple[CandidateExperimentPayload, ...]
    failed: bool


def derive_adaptive_retrieval_options(
    task: RetrievalOptimizationTask,
    observations: tuple[ToolObservation, ...],
) -> tuple[RetrievalDecisionOption, ...]:
    """Return the complete finite action set for one adaptive Planner turn.

    The model never constructs a ToolAction or FinishDecision. It chooses one
    option ID from this set, and the server maps it to a canonical decision.
    """

    if task.decision_policy != "adaptive_llm_v1":
        raise ValueError("adaptive retrieval options require the LLM policy")
    if task.candidate_variants != RETRIEVAL_PIPELINE_VARIANTS:
        raise ValueError("retrieval candidate space is not trusted")
    history = analyze_adaptive_retrieval_history(task, observations)
    if history.failed:
        return ()
    if history.baseline is None:
        return (
            RetrievalDecisionOption(
                option_id=DIAGNOSE_BASELINE_OPTION,
                decision=ToolAction(
                    tool_name=DIAGNOSE_BASELINE_TOOL,
                    arguments={"profile": task.profile},
                    reason_code="llm_diagnose_retrieval_baseline",
                ),
                description_code="establish_stage_baseline",
            ),
        )

    tested = {item.pipeline_variant for item in history.experiments}
    options: list[RetrievalDecisionOption] = []
    for variant in task.candidate_variants:
        if variant in tested:
            continue
        options.append(
            RetrievalDecisionOption(
                option_id=OPTION_BY_VARIANT[variant],
                decision=ToolAction(
                    tool_name=RUN_CANDIDATE_TOOL,
                    arguments={
                        "baseline_run_id": history.baseline.run_id,
                        "pipeline_variant": variant,
                    },
                    reason_code=RUN_REASON_BY_VARIANT[variant],
                ),
                description_code="test_unobserved_retrieval_candidate",
            )
        )

    passing = [item for item in history.experiments if item.gate.passed]
    if passing:
        selected = max(passing, key=lambda item: item.selection_key)
        options.append(
            RetrievalDecisionOption(
                option_id=FINISH_BEST_OPTION,
                decision=FinishDecision(
                    outcome=TerminalOutcome.PROPOSAL_READY,
                    evidence_refs=_successful_evidence(observations),
                    reason_code=FINISH_REASON_BY_VARIANT[selected.pipeline_variant],
                ),
                description_code="finish_with_best_gate_passing_candidate",
            )
        )
    elif len(history.experiments) == len(task.candidate_variants):
        options.append(
            RetrievalDecisionOption(
                option_id=FINISH_NO_SAFE_OPTION,
                decision=FinishDecision(
                    outcome=TerminalOutcome.NO_SAFE_IMPROVEMENT,
                    evidence_refs=_successful_evidence(observations),
                    reason_code="llm_no_safe_candidate",
                ),
                description_code="finish_after_all_candidates_failed_gates",
            )
        )
    if not options:
        raise ValueError("adaptive retrieval policy produced no safe decision")
    return tuple(options)


def analyze_adaptive_retrieval_history(
    task: RetrievalOptimizationTask,
    observations: tuple[ToolObservation, ...],
) -> AdaptiveRetrievalHistory:
    """Validate observed evidence without assuming a candidate execution order."""

    baseline: BaselineDiagnosisPayload | None = None
    experiments: list[CandidateExperimentPayload] = []
    failed = False
    for index, observation in enumerate(observations):
        if failed:
            raise ValueError("retrieval observations continue after a failed tool")
        if observation.status == "failed":
            _validate_failed_observation(observation)
            if index != len(observations) - 1:
                raise ValueError("failed retrieval observation must be final")
            failed = True
            continue
        _validate_success_observation(observation)
        if observation.tool_name == DIAGNOSE_BASELINE_TOOL:
            if baseline is not None or experiments:
                raise ValueError("retrieval baseline is duplicated or reordered")
            envelope = BaselineDiagnosisOutput.model_validate(
                {
                    "evidence_ref": observation.evidence_ref,
                    "payload": observation.payload,
                },
                strict=True,
            )
            if envelope.payload.profile != task.profile:
                raise ValueError("baseline profile is outside task scope")
            baseline = envelope.payload
            continue
        if observation.tool_name != RUN_CANDIDATE_TOOL or baseline is None:
            raise ValueError("retrieval candidate has no trusted baseline")
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
        if experiment.baseline_run_id != baseline.run_id:
            raise ValueError("candidate baseline does not match diagnosed baseline")
        if experiment.pipeline_variant not in task.candidate_variants:
            raise ValueError("candidate variant is outside the trusted space")
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
    return AdaptiveRetrievalHistory(
        baseline=baseline,
        experiments=tuple(experiments),
        failed=failed,
    )


def option_for_adaptive_decision(
    task: RetrievalOptimizationTask,
    observations: tuple[ToolObservation, ...],
    decision: AgentDecision,
) -> RetrievalDecisionOption:
    """Resolve a canonical decision back to its current server option."""

    matches = [
        option
        for option in derive_adaptive_retrieval_options(task, observations)
        if option.decision == decision
    ]
    if len(matches) != 1:
        raise ValueError("retrieval decision is outside the adaptive option set")
    return matches[0]


def _successful_evidence(observations: tuple[ToolObservation, ...]) -> list[str]:
    return [
        item.evidence_ref
        for item in observations
        if item.status == "succeeded" and item.evidence_ref is not None
    ]


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
