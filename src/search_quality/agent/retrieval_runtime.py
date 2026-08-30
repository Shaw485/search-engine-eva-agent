"""Run the stage-aware retrieval optimizer through Runtime, Trace and Replay."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from search_quality.evaluation.artifacts import require_clean_code_revision

from .contracts import (
    RETRIEVAL_PIPELINE_VARIANTS,
    PlannerDecisionAudit,
    RetrievalOptimizationTask,
    TerminalOutcome,
)
from .planner import Planner
from .replay import TraceReplayer
from .retrieval_planner import ObservationDrivenRetrievalPlanner
from .retrieval_tools import (
    DIAGNOSE_BASELINE_TOOL,
    RETRIEVAL_TOOL_CAPABILITIES,
    RUN_CANDIDATE_TOOL,
    StageRetrievalTools,
)
from .runtime import AgentRuntime, RuntimePolicy
from .trace import AgentTrace, TraceStore

logger = logging.getLogger("search_quality.agent_runtime.retrieval")
AGENT_RUN_SUMMARY_SCHEMA_VERSION = "retrieval-agent-run-summary-v2"


def generate_retrieval_runtime_analysis(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    profile_id: Literal["smoke"] = "smoke",
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
    planner: Planner | None = None,
) -> dict[str, Any]:
    """Return the existing analysis evidence plus a replay-validated Trace summary."""

    if profile_id != "smoke":
        raise ValueError("retrieval Runtime analysis is currently smoke-only")
    root = Path(project_root).resolve(strict=True)
    run_store = _resolve_artifact_root(root, artifact_root)
    trace_store = TraceStore(run_store / "agent-traces")
    domain_tools = StageRetrievalTools(
        project_root=root,
        artifact_root=run_store,
        revision_provider=revision_provider,
    )
    resolved_planner = planner or ObservationDrivenRetrievalPlanner()
    decision_policy = getattr(
        resolved_planner,
        "decision_policy",
        "fixed_sequence_v1",
    )
    if decision_policy not in {"fixed_sequence_v1", "adaptive_llm_v1"}:
        raise ValueError("retrieval Planner decision policy is invalid")
    llm_mode = decision_policy == "adaptive_llm_v1"
    runtime = AgentRuntime(
        planner=resolved_planner,
        tools=domain_tools.build_registry(),
        trace_store=trace_store,
        policy=RuntimePolicy(
            max_steps=6 if llm_mode else 8,
            max_tool_calls=4 if llm_mode else 6,
            max_run_creations=4 if llm_mode else 5,
            max_failures=1 if llm_mode else 3,
            max_same_action_attempts=1 if llm_mode else 2,
            max_elapsed_ms=120_000,
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        ),
    )
    task = RetrievalOptimizationTask(
        task_id="stage-retrieval-smoke",
        decision_policy=decision_policy,
    )
    terminal = runtime.run(task)
    if terminal.state != "completed" or terminal.outcome not in {
        TerminalOutcome.PROPOSAL_READY,
        TerminalOutcome.NO_SAFE_IMPROVEMENT,
    }:
        logger.error(
            "retrieval_runtime_analysis_incomplete",
            extra={
                "agent_trace_id": terminal.trace_id,
                "error_code": terminal.reason_code,
                "terminal_state": terminal.state,
                "tool_calls_used": terminal.tool_calls_used,
            },
        )
        raise RuntimeError("retrieval Runtime did not produce complete evidence")

    trace = TraceReplayer(trace_store).replay_trace(terminal.trace_id)
    if trace.terminal != terminal:
        raise RuntimeError("retrieval Runtime Replay does not match terminal result")
    analysis = domain_tools.build_analysis_response(terminal)
    analysis["agent_run"] = _agent_run_summary(trace)
    logger.info(
        "retrieval_runtime_analysis_completed",
        extra={
            "agent_trace_id": terminal.trace_id,
            "changed_query_example_count": len(analysis["changed_query_examples"]),
            "comparison_id": analysis["comparison_id"],
            "experiment_count": len(analysis["experiments"]),
            "improvement_example_count": sum(
                item["outcome"] == "improvement"
                for item in analysis["changed_query_examples"]
            ),
            "outcome": terminal.outcome.value,
            "pipeline_run_id": analysis["retrieval_run_id"],
            "regression_example_count": sum(
                item["outcome"] == "regression"
                for item in analysis["changed_query_examples"]
            ),
            "tool_calls_used": terminal.tool_calls_used,
        },
    )
    return analysis


def _resolve_artifact_root(
    project_root: Path,
    artifact_root: str | Path | None,
) -> Path:
    requested = project_root / "runs" if artifact_root is None else Path(artifact_root)
    if not requested.is_absolute():
        raise ValueError("retrieval Runtime artifact root must be absolute")
    if requested.is_symlink():
        raise ValueError("retrieval Runtime artifact root must not be a symbolic link")
    requested.mkdir(parents=True, exist_ok=True)
    resolved = requested.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("retrieval Runtime artifact root must be a directory")
    return resolved


def _agent_run_summary(trace: AgentTrace) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    pending_audit: PlannerDecisionAudit | None = None
    audits: list[PlannerDecisionAudit] = []
    for event in trace.events:
        if event.event_type == "action_selected":
            if pending is not None or event.decision is None:
                raise ValueError("retrieval Trace action sequence is malformed")
            pending = event.decision
            pending_audit = _validated_audit(event.planner_audit)
            if pending_audit is not None:
                audits.append(pending_audit)
            continue
        if event.event_type != "tool_observed":
            if event.event_type == "run_completed":
                terminal_audit = _validated_audit(event.planner_audit)
                if terminal_audit is not None:
                    audits.append(terminal_audit)
            continue
        if pending is None or event.observation is None:
            raise ValueError("retrieval Trace observation sequence is malformed")
        actions.append(
            _summarize_action(
                pending,
                event.observation,
                len(actions) + 1,
                pending_audit,
            )
        )
        pending = None
        pending_audit = None
    if pending is not None or len(actions) != trace.terminal.tool_calls_used:
        raise ValueError("retrieval Trace action count is inconsistent")
    llm_mode = trace.planner_id == "llm-retrieval-planner-v1"
    if llm_mode and len(audits) != trace.terminal.steps_used:
        raise ValueError("retrieval LLM Trace audit count is inconsistent")
    if not llm_mode and audits:
        raise ValueError("deterministic retrieval Trace contains LLM audit data")
    return {
        "actions": actions,
        "llm_usage": _llm_usage_summary(audits) if llm_mode else None,
        "outcome": trace.terminal.outcome.value,
        "planner_id": trace.planner_id,
        "planner_mode": "llm" if llm_mode else "deterministic",
        "reason_code": trace.terminal.reason_code,
        "replay_mode": "recorded_trace",
        "runtime_id": trace.runtime_id,
        "schema_version": AGENT_RUN_SUMMARY_SCHEMA_VERSION,
        "state": trace.terminal.state,
        "steps_used": trace.terminal.steps_used,
        "tool_calls_used": trace.terminal.tool_calls_used,
        "trace_id": trace.trace_id,
    }


def _summarize_action(
    decision: dict[str, Any],
    observation: dict[str, Any],
    sequence: int,
    audit: PlannerDecisionAudit | None = None,
) -> dict[str, Any]:
    """Expose one bounded action pair without leaking tool payloads."""

    payload = observation.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("retrieval Trace observation payload is malformed")
    tool_name = decision.get("tool_name")
    status = observation.get("status")
    retryable = observation.get("retryable")
    if status not in {"succeeded", "failed"} or not isinstance(retryable, bool):
        raise ValueError("retrieval Trace observation status is malformed")

    if tool_name == DIAGNOSE_BASELINE_TOOL:
        pipeline_variant = None
    elif tool_name == RUN_CANDIDATE_TOOL:
        arguments = decision.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("retrieval Trace action arguments are malformed")
        pipeline_variant = arguments.get("pipeline_variant")
        if pipeline_variant not in RETRIEVAL_PIPELINE_VARIANTS:
            raise ValueError("retrieval Trace pipeline variant is malformed")
    else:
        raise ValueError("retrieval Trace contains an unsupported tool")

    if status == "failed":
        if observation.get("evidence_ref") is not None or payload:
            raise ValueError("failed retrieval Trace action contains evidence")
        gate_passed = None
        failed_gates: list[str] = []
    elif tool_name == DIAGNOSE_BASELINE_TOOL:
        gate_passed = None
        failed_gates = []
    else:
        if payload.get("pipeline_variant") != pipeline_variant:
            raise ValueError("retrieval Trace action and evidence variant differ")
        gate = payload.get("gate")
        if not isinstance(gate, dict):
            raise ValueError("retrieval Trace gate summary is missing")
        gate_passed = gate.get("passed")
        failed_gates = gate.get("failed_gates")
        if not isinstance(gate_passed, bool) or not isinstance(failed_gates, list):
            raise ValueError("retrieval Trace gate summary is malformed")

    return {
        "decision_source": "llm" if audit is not None else "deterministic",
        "evidence_ref": observation.get("evidence_ref"),
        "failed_gates": failed_gates,
        "gate_passed": gate_passed,
        "pipeline_variant": pipeline_variant,
        "reason_code": decision.get("reason_code"),
        "retryable": retryable,
        "sequence": sequence,
        "selected_option_id": (audit.selected_option_id if audit is not None else None),
        "status": status,
        "tool_name": tool_name,
        "model_call": (
            {
                "duration_ms": audit.duration_ms,
                "input_tokens": audit.input_tokens,
                "model_id": audit.model_id,
                "output_tokens": audit.output_tokens,
                "total_tokens": audit.total_tokens,
            }
            if audit is not None
            else None
        ),
    }


def _validated_audit(value: dict[str, Any] | None) -> PlannerDecisionAudit | None:
    if value is None:
        return None
    return PlannerDecisionAudit.model_validate(value, strict=True)


def _llm_usage_summary(audits: list[PlannerDecisionAudit]) -> dict[str, Any]:
    if not audits:
        raise ValueError("retrieval LLM Trace has no model-call audit")
    first = audits[0]
    if any(
        item.provider_id != first.provider_id
        or item.model_id != first.model_id
        or item.prompt_version != first.prompt_version
        or item.planner_config_sha256 != first.planner_config_sha256
        for item in audits
    ):
        raise ValueError("retrieval LLM configuration changed inside one Trace")
    input_tokens = sum(item.input_tokens for item in audits)
    output_tokens = sum(item.output_tokens for item in audits)
    return {
        "input_tokens": input_tokens,
        "model_calls": len(audits),
        "model_id": first.model_id,
        "output_tokens": output_tokens,
        "prompt_version": first.prompt_version,
        "provider_id": first.provider_id,
        "terminal_option_id": audits[-1].selected_option_id,
        "total_tokens": input_tokens + output_tokens,
    }
