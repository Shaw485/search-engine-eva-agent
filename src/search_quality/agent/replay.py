"""Offline Agent Trace replay that never invokes a Planner or tool."""

from __future__ import annotations

import hashlib
import logging

from search_quality.observability import logging_context

from .contracts import (
    AgentState,
    FinishDecision,
    PlannerDecisionAudit,
    RetrievalOptimizationTask,
    TerminalOutcome,
    ToolAction,
    ToolObservation,
)
from .errors import AgentPolicyError, AgentReplayError
from .grounding import validate_action_scope, validate_finish_grounding
from .reporting import build_terminal_report
from .trace import (
    ZERO_HASH,
    AgentTrace,
    TraceStore,
    compute_event_hash,
    compute_terminal_hash,
    compute_trace_context_hash,
    re_fullmatch_trace_id,
)

logger = logging.getLogger("search_quality.agent_replay")

ALLOWED_TRANSITIONS = frozenset(
    {
        (AgentState.PLANNING, "action_selected", AgentState.ACTING),
        (AgentState.DECIDING, "action_selected", AgentState.ACTING),
        (AgentState.ACTING, "tool_observed", AgentState.OBSERVING),
        (AgentState.OBSERVING, "observation_processed", AgentState.DECIDING),
        (AgentState.PLANNING, "run_completed", AgentState.COMPLETED),
        (AgentState.DECIDING, "run_completed", AgentState.COMPLETED),
        (AgentState.PLANNING, "run_failed", AgentState.FAILED),
        (AgentState.ACTING, "run_failed", AgentState.FAILED),
        (AgentState.OBSERVING, "run_failed", AgentState.FAILED),
        (AgentState.DECIDING, "run_failed", AgentState.FAILED),
    }
)


class TraceReplayer:
    def __init__(self, store: TraceStore) -> None:
        self.store = store

    def replay(self, trace_id: str):
        return self.replay_trace(trace_id).terminal

    def replay_trace(self, trace_id: str) -> AgentTrace:
        """Return the exact validated snapshot without loading it a second time."""

        safe_trace_id = (
            trace_id
            if isinstance(trace_id, str) and re_fullmatch_trace_id(trace_id)
            else "invalid"
        )
        with logging_context(trace_id=safe_trace_id, operation="agent_trace_replay"):
            logger.info("agent_replay_started")
            try:
                if safe_trace_id == "invalid":
                    raise ValueError("invalid Trace ID")
                trace = self.store.load(trace_id)
                observations: list[ToolObservation] = []
                state = AgentState.PLANNING
                previous_hash = ZERO_HASH
                terminal_event = None
                pending_tool_name: str | None = None
                pending_scope_error: str | None = None
                pending_registry_error: str | None = None
                llm_audit_binding: tuple[str, ...] | None = None
                action_count = 0
                for expected_sequence, event in enumerate(trace.events, start=1):
                    if event.sequence != expected_sequence:
                        raise ValueError("Trace sequence is not contiguous")
                    if event.previous_hash != previous_hash:
                        raise AgentReplayError(
                            "trace_hash_mismatch", "Trace hash chain is broken"
                        )
                    payload = event.model_dump(mode="json", exclude={"event_hash"})
                    valid_event_hashes = {compute_event_hash(payload)}
                    if payload.get("planner_audit") is None:
                        payload.pop("planner_audit", None)
                        valid_event_hashes.add(compute_event_hash(payload))
                    if event.event_hash not in valid_event_hashes:
                        raise AgentReplayError(
                            "trace_hash_mismatch", "Trace event hash does not match"
                        )
                    if event.state_before != state:
                        raise ValueError("Trace state continuity is broken")
                    transition = (
                        event.state_before,
                        event.event_type,
                        event.state_after,
                    )
                    if transition not in ALLOWED_TRANSITIONS:
                        raise ValueError("Trace contains an invalid state transition")
                    planner_audit = None
                    if event.planner_audit is not None:
                        if event.event_type not in {
                            "action_selected",
                            "run_completed",
                        }:
                            raise ValueError(
                                "Trace Planner audit is attached to a non-decision event"
                            )
                        planner_audit = PlannerDecisionAudit.model_validate(
                            event.planner_audit,
                            strict=True,
                        )
                    if (
                        trace.planner_id == "llm-retrieval-planner-v1"
                        and event.event_type in {"action_selected", "run_completed"}
                        and planner_audit is None
                    ):
                        raise ValueError(
                            "LLM Trace decision is missing its audit record"
                        )
                    is_terminal_event = event.event_type in {
                        "run_completed",
                        "run_failed",
                    }
                    if is_terminal_event:
                        if expected_sequence != len(trace.events):
                            raise ValueError("Trace terminal event must be final")
                        if event.terminal_sha256 is None:
                            raise ValueError("Trace terminal event is not bound")
                        if event.context_sha256 is None:
                            raise ValueError("Trace context is not bound")
                        if pending_tool_name is not None:
                            raise ValueError("Trace action has no observation")
                        terminal_event = event
                    elif (
                        event.terminal_sha256 is not None
                        or event.context_sha256 is not None
                    ):
                        raise ValueError("Trace non-terminal event binds final data")
                    if event.event_type == "action_selected":
                        if not event.decision or event.decision.get("kind") != "tool":
                            raise ValueError("Trace action event is malformed")
                        if pending_tool_name is not None:
                            raise ValueError("Trace action has no observation")
                        action = ToolAction.model_validate(event.decision)
                        llm_audit_binding = self._validate_llm_option_binding(
                            trace=trace,
                            observations=tuple(observations),
                            decision=action,
                            audit=planner_audit,
                            expected_binding=llm_audit_binding,
                        )
                        pending_tool_name = action.tool_name
                        if action.tool_name not in trace.tool_names:
                            pending_registry_error = "tool_not_allowed"
                        try:
                            validate_action_scope(
                                trace.task,
                                action,
                                tuple(observations),
                            )
                        except AgentPolicyError as exc:
                            pending_scope_error = exc.code
                        action_count += 1
                    if event.event_type == "tool_observed":
                        if event.observation is None:
                            raise ValueError("Trace observation event is malformed")
                        observation = ToolObservation.model_validate(event.observation)
                        if (
                            pending_tool_name is None
                            or observation.tool_name != pending_tool_name
                        ):
                            raise ValueError(
                                "Trace observation does not match its action"
                            )
                        expected_policy_error = (
                            pending_scope_error or pending_registry_error
                        )
                        if expected_policy_error is not None and (
                            observation.status != "failed"
                            or observation.error_code != expected_policy_error
                        ):
                            raise ValueError(
                                "Trace task-scope violation was not enforced"
                            )
                        if (
                            hashlib.sha256(observation.canonical_payload()).hexdigest()
                            != observation.sha256
                        ):
                            raise AgentReplayError(
                                "trace_hash_mismatch",
                                "Trace observation hash does not match",
                            )
                        observations.append(observation)
                        pending_tool_name = None
                        pending_scope_error = None
                        pending_registry_error = None
                    if event.event_type == "run_completed":
                        if not event.decision or event.decision.get("kind") != "finish":
                            raise ValueError("Trace completion event is malformed")
                    state = event.state_after
                    previous_hash = event.event_hash

                if terminal_event is None:
                    raise ValueError("Trace terminal event is missing")
                if trace.terminal.trace_id != trace.trace_id:
                    raise ValueError("Trace terminal ID does not match")
                if (
                    compute_trace_context_hash(
                        trace_id=trace.trace_id,
                        planner_id=trace.planner_id,
                        task=trace.task,
                        policy=trace.policy,
                        tool_names=list(trace.tool_names),
                    )
                    != terminal_event.context_sha256
                ):
                    raise ValueError("Trace context hash does not match")
                if (
                    compute_terminal_hash(trace.terminal)
                    != terminal_event.terminal_sha256
                ):
                    raise ValueError("Trace terminal hash does not match")

                terminal_state = AgentState(trace.terminal.state)
                if state != terminal_state or state not in {
                    AgentState.COMPLETED,
                    AgentState.FAILED,
                }:
                    raise ValueError("Trace terminal state does not match")
                if terminal_event.event_type == "run_completed":
                    if terminal_state != AgentState.COMPLETED:
                        raise ValueError("Trace completion state does not match")
                    decision = FinishDecision.model_validate(terminal_event.decision)
                    terminal_audit = (
                        PlannerDecisionAudit.model_validate(
                            terminal_event.planner_audit,
                            strict=True,
                        )
                        if terminal_event.planner_audit is not None
                        else None
                    )
                    llm_audit_binding = self._validate_llm_option_binding(
                        trace=trace,
                        observations=tuple(observations),
                        decision=decision,
                        audit=terminal_audit,
                        expected_binding=llm_audit_binding,
                    )
                    if (
                        decision.outcome != trace.terminal.outcome
                        or decision.reason_code != trace.terminal.reason_code
                        or decision.evidence_refs != trace.terminal.evidence_refs
                    ):
                        raise ValueError(
                            "Trace completion decision does not match terminal"
                        )
                    try:
                        validate_finish_grounding(trace.task, decision, observations)
                    except AgentPolicyError as exc:
                        raise ValueError(
                            "Trace terminal evidence is outside task scope"
                        ) from exc
                else:
                    if terminal_event.decision is not None:
                        raise ValueError("Trace failure event contains a decision")
                    if (
                        terminal_state != AgentState.FAILED
                        or trace.terminal.outcome != TerminalOutcome.INCONCLUSIVE
                    ):
                        raise ValueError("Trace failure terminal is malformed")
                if trace.terminal.tool_calls_used != action_count:
                    raise ValueError("Trace tool call count does not replay")
                unrecorded_decision_failures = {
                    "repeated_action_rejected",
                    "run_creation_budget_exhausted",
                    "tool_call_budget_exhausted",
                    "ungrounded_finish_rejected",
                }
                expected_steps = action_count + int(
                    terminal_event.event_type == "run_completed"
                    or trace.terminal.reason_code in unrecorded_decision_failures
                )
                if trace.terminal.steps_used != expected_steps:
                    raise ValueError("Trace step count does not replay")
                max_steps = trace.policy.get("max_steps")
                max_tool_calls = trace.policy.get("max_tool_calls")
                max_run_creations = trace.policy.get("max_run_creations")
                if (
                    isinstance(max_steps, bool)
                    or not isinstance(max_steps, int)
                    or max_steps < 1
                    or isinstance(max_tool_calls, bool)
                    or not isinstance(max_tool_calls, int)
                    or max_tool_calls < 1
                    or isinstance(max_run_creations, bool)
                    or not isinstance(max_run_creations, int)
                    or max_run_creations < 1
                ):
                    raise ValueError("Trace runtime policy is malformed")
                if (
                    trace.terminal.steps_used > max_steps
                    or trace.terminal.tool_calls_used > max_tool_calls
                ):
                    raise ValueError("Trace terminal counts exceed policy")
                run_creation_count = sum(
                    event.decision is not None
                    and event.decision.get("tool_name")
                    in {
                        "run_ranker",
                        "diagnose_baseline_retrieval",
                        "run_retrieval_candidate",
                    }
                    for event in trace.events
                    if event.event_type == "action_selected"
                )
                if run_creation_count > max_run_creations:
                    raise ValueError("Trace Run creations exceed policy")
                if isinstance(trace.task, RetrievalOptimizationTask):
                    from .retrieval_tools import (
                        DIAGNOSE_BASELINE_TOOL,
                        RETRIEVAL_TOOL_CAPABILITIES,
                        RUN_CANDIDATE_TOOL,
                    )

                    if trace.tool_names != sorted(
                        {DIAGNOSE_BASELINE_TOOL, RUN_CANDIDATE_TOOL}
                    ):
                        raise ValueError("Retrieval Trace tool registry is not minimal")
                    if trace.policy.get("allowed_capabilities") != sorted(
                        RETRIEVAL_TOOL_CAPABILITIES
                    ):
                        raise ValueError("Retrieval Trace capabilities are not minimal")
                observed_refs = {
                    item.evidence_ref
                    for item in observations
                    if item.status == "succeeded" and item.evidence_ref is not None
                }
                if not set(trace.terminal.evidence_refs) <= observed_refs:
                    raise ValueError("Trace terminal evidence is not grounded")
                expected_report = build_terminal_report(
                    task=trace.task,
                    outcome=trace.terminal.outcome,
                    reason_code=trace.terminal.reason_code,
                    observations=tuple(observations),
                    evidence_refs=trace.terminal.evidence_refs,
                )
                if expected_report != trace.terminal.report:
                    raise ValueError("Trace terminal report does not replay")
            except Exception as exc:
                error_code = (
                    exc.code
                    if isinstance(exc, AgentReplayError)
                    else "trace_replay_failed"
                )
                logger.error(
                    "agent_replay_failed",
                    extra={"error_code": error_code},
                )
                raise
            logger.info(
                "agent_replay_completed",
                extra={
                    "event_count": len(trace.events),
                    "terminal_state": trace.terminal.state,
                },
            )
            return trace

    @staticmethod
    def _validate_llm_option_binding(
        *,
        trace: AgentTrace,
        observations: tuple[ToolObservation, ...],
        decision: ToolAction | FinishDecision,
        audit: PlannerDecisionAudit | None,
        expected_binding: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if trace.planner_id != "llm-retrieval-planner-v1":
            if audit is not None:
                raise ValueError("Non-LLM Trace contains LLM Planner audit data")
            return None
        if audit is None or not isinstance(trace.task, RetrievalOptimizationTask):
            raise ValueError("LLM Trace decision audit is malformed")
        from .retrieval_policy import derive_adaptive_retrieval_options

        options = derive_adaptive_retrieval_options(trace.task, observations)
        if audit.option_count != len(options):
            raise ValueError("LLM Trace option count does not replay")
        matching = [option for option in options if option.decision == decision]
        if len(matching) != 1:
            raise ValueError("retrieval decision is outside the adaptive option set")
        option = matching[0]
        if option.option_id != audit.selected_option_id:
            raise ValueError("LLM Trace option does not match its canonical decision")
        binding = (
            audit.schema_version,
            audit.source,
            audit.provider_id,
            audit.model_id,
            audit.prompt_version,
            audit.decision_schema_version,
            audit.data_policy,
            audit.planner_config_sha256,
        )
        if expected_binding is not None and binding != expected_binding:
            raise ValueError(
                "LLM Trace Planner configuration changes between decisions"
            )
        return binding
