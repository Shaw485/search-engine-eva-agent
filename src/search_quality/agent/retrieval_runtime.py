"""Run the stage-aware retrieval optimizer through Runtime, Trace and Replay."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from search_quality.evaluation.artifacts import require_clean_code_revision

from .contracts import (
    RETRIEVAL_PIPELINE_VARIANTS,
    RetrievalOptimizationTask,
    TerminalOutcome,
)
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
AGENT_RUN_SUMMARY_SCHEMA_VERSION = "retrieval-agent-run-summary-v1"


def generate_retrieval_runtime_analysis(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    profile_id: Literal["smoke"] = "smoke",
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
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
    planner = ObservationDrivenRetrievalPlanner()
    runtime = AgentRuntime(
        planner=planner,
        tools=domain_tools.build_registry(),
        trace_store=trace_store,
        policy=RuntimePolicy(
            max_steps=8,
            max_tool_calls=6,
            max_run_creations=5,
            max_failures=3,
            max_same_action_attempts=2,
            max_elapsed_ms=120_000,
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        ),
    )
    task = RetrievalOptimizationTask(task_id="stage-retrieval-smoke")
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
    for event in trace.events:
        if event.event_type == "action_selected":
            if pending is not None or event.decision is None:
                raise ValueError("retrieval Trace action sequence is malformed")
            pending = event.decision
            continue
        if event.event_type != "tool_observed":
            continue
        if pending is None or event.observation is None:
            raise ValueError("retrieval Trace observation sequence is malformed")
        actions.append(_summarize_action(pending, event.observation, len(actions) + 1))
        pending = None
    if pending is not None or len(actions) != trace.terminal.tool_calls_used:
        raise ValueError("retrieval Trace action count is inconsistent")
    return {
        "actions": actions,
        "outcome": trace.terminal.outcome.value,
        "planner_id": trace.planner_id,
        "reason_code": trace.terminal.reason_code,
        "replay_supported": True,
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
        "evidence_ref": observation.get("evidence_ref"),
        "failed_gates": failed_gates,
        "gate_passed": gate_passed,
        "pipeline_variant": pipeline_variant,
        "reason_code": decision.get("reason_code"),
        "retryable": retryable,
        "sequence": sequence,
        "status": status,
        "tool_name": tool_name,
    }
