"""Run the fixed Stage 5 Agent Eval suite and publish a deterministic scorecard."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from search_quality.agent.contracts import RetrievalOptimizationTask
from search_quality.agent.errors import AgentReplayError
from search_quality.agent.replay import TraceReplayer
from search_quality.agent.retrieval_planner import ObservationDrivenRetrievalPlanner
from search_quality.agent.retrieval_tools import (
    RETRIEVAL_TOOL_CAPABILITIES,
    StageRetrievalTools,
)
from search_quality.agent.runtime import AgentRuntime, RuntimePolicy
from search_quality.agent.trace import AgentTrace, TraceStore
from search_quality.evaluation.artifacts import (
    require_clean_code_revision,
    write_immutable_json,
)
from search_quality.observability import logging_context, new_trace_id

from .artifacts import store_agent_eval_artifacts, trusted_agent_eval_root
from .catalog import load_agent_eval_suite
from .contracts import (
    AgentEvalEvidence,
    AgentEvalExecutionReceipt,
    AgentEvalExecutionTask,
    AgentEvalRun,
    FixedWorkflowResult,
)
from .judge import build_metrics, build_subject_summaries, grade_task, suite_passes
from .scenarios import (
    HandlerRecordingRegistry,
    ScriptedRetrievalTools,
    canonical_evidence_from_trace,
    planner_for_case,
    run_fixed_workflow,
    subject_kind_for_case,
)

logger = logging.getLogger("search_quality.agent_eval")
SUBJECT_ID = "stage-aware-retrieval-agent-v1"
MAX_AGENT_EVAL_STORE_BYTES = 2 * 1024 * 1024 * 1024
_FORBIDDEN_NAMES = frozenset(
    {
        "active-strategy.json",
        "search-strategies",
        "strategy-decisions",
        "strategy-proposals",
    }
)


def run_agent_eval_suite(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    suite_id: str = "stage5-retrieval-v1",
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> AgentEvalRun:
    """Execute all 12 fixed tasks; partial runs cannot become formal evidence."""

    root = Path(project_root).resolve(strict=True)
    run_root = root / "runs" if artifact_root is None else Path(artifact_root)
    suite, suite_sha256 = load_agent_eval_suite(project_root=root, suite_id=suite_id)
    code_revision = revision_provider(root).strip()
    if len(code_revision) != 40 or any(
        character not in "0123456789abcdef" for character in code_revision
    ):
        raise ValueError("Agent Eval code revision must be a full Git SHA")
    base = trusted_agent_eval_root(run_root)
    run_root = base.parent
    _ensure_artifact_capacity(base)
    authority_before = _authority_snapshot(run_root)
    trace_store = TraceStore(base / "traces")
    started_at = _utc_now()
    started = time.perf_counter()
    execution_tasks: list[AgentEvalExecutionTask] = []
    results = []
    workflow_results: list[FixedWorkflowResult] = []

    logger.info(
        "agent_eval_suite_started",
        extra={"suite_id": suite.suite_id, "task_count": len(suite.tasks)},
    )
    canonical_case = suite.tasks[0]
    canonical_started = time.perf_counter()
    with logging_context(operation="agent_eval_task", task_id=canonical_case.task_id):
        logger.info(
            "agent_eval_task_started",
            extra={"category": canonical_case.category},
        )
        task_authority_before = _authority_snapshot(run_root)
        try:
            real_tools = StageRetrievalTools(
                project_root=root,
                artifact_root=base / "source-evidence",
                revision_provider=lambda _root: code_revision,
            )
            real_registry = HandlerRecordingRegistry(real_tools.build_registry())
            terminal = _runtime(
                case=canonical_case,
                planner=ObservationDrivenRetrievalPlanner(),
                registry=real_registry,
                trace_store=trace_store,
            ).run(RetrievalOptimizationTask(task_id=canonical_case.task_id))
            canonical_trace = trace_store.load(terminal.trace_id)
            result = _replay_and_grade(
                case=canonical_case,
                trace=canonical_trace,
                trace_store=trace_store,
                base=base,
                task_authority_before=task_authority_before,
                authority_root=run_root,
                handler_ledger_counter=lambda: real_registry.ledger,
                protected_profile_reads=lambda: real_registry.protected_profile_reads,
            )
        except Exception as exc:
            logger.error(
                "agent_eval_task_failed",
                extra={
                    "duration_ms": round(_elapsed_ms(canonical_started), 3),
                    "error_code": "agent_eval_task_failed",
                    "error_type": type(exc).__name__,
                    "failure_stage": "runtime_or_grade",
                },
            )
            raise
        results.append(result)
        execution_tasks.append(
            AgentEvalExecutionTask(
                task_id=canonical_case.task_id,
                trace_id=terminal.trace_id,
                subject_kind=subject_kind_for_case(canonical_case),
                planner_id=canonical_trace.planner_id,
                duration_ms=_elapsed_ms(canonical_started),
            )
        )
        logger.info(
            "agent_eval_task_completed",
            extra={
                "passed": result.passed,
                "duration_ms": round(_elapsed_ms(canonical_started), 3),
                "planner_id": result.actual_planner_id,
                "reason_code": result.reason_code,
                "subject_kind": result.subject_kind,
                "tool_calls_used": result.tool_calls_used,
            },
        )
    canonical_evidence = canonical_evidence_from_trace(canonical_trace)

    for case in suite.tasks[1:]:
        task_started = time.perf_counter()
        with logging_context(operation="agent_eval_task", task_id=case.task_id):
            logger.info("agent_eval_task_started", extra={"category": case.category})
            task_authority_before = _authority_snapshot(run_root)
            try:
                backend = ScriptedRetrievalTools(case, canonical_evidence)
                recording_registry = HandlerRecordingRegistry(backend.build_registry())
                terminal = _runtime(
                    case=case,
                    planner=planner_for_case(case),
                    registry=recording_registry,
                    trace_store=trace_store,
                ).run(RetrievalOptimizationTask(task_id=case.task_id))
                trace = trace_store.load(terminal.trace_id)
                result = _replay_and_grade(
                    case=case,
                    trace=trace,
                    trace_store=trace_store,
                    base=base,
                    task_authority_before=task_authority_before,
                    authority_root=run_root,
                    handler_ledger_counter=lambda registry=recording_registry: (
                        registry.ledger
                    ),
                    protected_profile_reads=lambda registry=recording_registry: (
                        registry.protected_profile_reads
                    ),
                )
            except Exception as exc:
                logger.error(
                    "agent_eval_task_failed",
                    extra={
                        "duration_ms": round(_elapsed_ms(task_started), 3),
                        "error_code": "agent_eval_task_failed",
                        "error_type": type(exc).__name__,
                        "failure_stage": "runtime_or_grade",
                    },
                )
                raise
            results.append(result)
            execution_tasks.append(
                AgentEvalExecutionTask(
                    task_id=case.task_id,
                    trace_id=terminal.trace_id,
                    subject_kind=subject_kind_for_case(case),
                    planner_id=trace.planner_id,
                    duration_ms=_elapsed_ms(task_started),
                )
            )
            logger.info(
                "agent_eval_task_completed",
                extra={
                    "passed": result.passed,
                    "duration_ms": round(_elapsed_ms(task_started), 3),
                    "planner_id": result.actual_planner_id,
                    "reason_code": result.reason_code,
                    "subject_kind": result.subject_kind,
                    "tool_calls_used": result.tool_calls_used,
                },
            )

    for case in suite.tasks:
        if not case.oracle.workflow_applicable:
            workflow_results.append(
                FixedWorkflowResult(
                    task_id=case.task_id,
                    applicable=False,
                    success=None,
                    tool_calls_used=None,
                    outcome_code=None,
                )
            )
            continue
        workflow = run_fixed_workflow(case, canonical_evidence)
        if (
            workflow.success != case.oracle.workflow_success
            or workflow.tool_calls_used != case.oracle.workflow_tool_calls
        ):
            raise ValueError("fixed workflow result does not match its static oracle")
        workflow_results.append(
            FixedWorkflowResult(
                task_id=case.task_id,
                applicable=True,
                success=workflow.success,
                tool_calls_used=workflow.tool_calls_used,
                outcome_code=workflow.outcome_code,
            )
        )

    authority_after = _authority_snapshot(run_root)
    global_strategy_writes = _authority_change_count(
        authority_before,
        authority_after,
    )
    measured_strategy_writes = sum(item.strategy_writes for item in results)
    if global_strategy_writes != measured_strategy_writes:
        raise RuntimeError("Agent Eval authority changes are not task-attributed")
    measured_protected_reads = sum(item.protected_profile_reads for item in results)

    metrics = build_metrics(
        suite=suite,
        results=results,
        workflows=workflow_results,
    )
    metrics = metrics.model_copy(
        update={
            "protected_profile_read_count": measured_protected_reads,
            "strategy_write_count": measured_strategy_writes,
        }
    )
    evidence_body = {
        "code_revision": code_revision,
        "complete_suite": len(results) == len(suite.tasks),
        "fixed_workflow": [item.model_dump(mode="json") for item in workflow_results],
        "formal_passed": suite_passes(suite, metrics),
        "limitations": [
            "scripted_failures_do_not_prove_worker_deadline_enforcement",
            "contract_fixtures_test_runtime_behavior_not_search_quality",
            "grounded_claim_rate_v1_is_terminal_grounding_proxy",
        ],
        "metrics": metrics.model_dump(mode="json"),
        "production_planner_id": "stage-aware-retrieval-planner-v1",
        "profile": "smoke",
        "protected_profile_reads": measured_protected_reads,
        "runtime_id": "search-agent-runtime-v1",
        "schema_version": "agent-eval-evidence-v1",
        "strategy_writes": measured_strategy_writes,
        "subject_id": SUBJECT_ID,
        "subject_summaries": [
            item.model_dump(mode="json") for item in build_subject_summaries(results)
        ],
        "suite_id": suite.suite_id,
        "suite_sha256": suite_sha256,
        "tasks": [item.model_dump(mode="json") for item in results],
    }
    evidence = AgentEvalEvidence.model_validate(
        {
            **evidence_body,
            "evidence_id": f"agent-eval-{_digest(evidence_body)[:12]}",
        }
    )
    completed_at = _utc_now()
    execution = AgentEvalExecutionReceipt(
        execution_id=f"agent-eval-execution-{new_trace_id()}",
        evidence_id=evidence.evidence_id,
        suite_id=suite.suite_id,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        duration_ms=_elapsed_ms(started),
        tasks=execution_tasks,
    )
    evidence_path, execution_path = store_agent_eval_artifacts(
        artifact_root=run_root,
        evidence=evidence,
        execution=execution,
    )
    logger.info(
        "agent_eval_suite_completed",
        extra={
            "evidence_id": evidence.evidence_id,
            "formal_passed": evidence.formal_passed,
            "task_count": len(results),
            "task_success_rate": metrics.task_success_rate,
            "duration_ms": round(_elapsed_ms(started), 3),
        },
    )
    return AgentEvalRun(
        evidence=evidence,
        execution=execution,
        evidence_path=str(evidence_path),
        execution_path=str(execution_path),
    )


def _runtime(*, case, planner, registry, trace_store: TraceStore) -> AgentRuntime:
    return AgentRuntime(
        planner=planner,
        tools=registry,
        trace_store=trace_store,
        policy=RuntimePolicy(
            max_steps=case.max_steps,
            max_tool_calls=6,
            max_run_creations=5,
            max_failures=3,
            max_same_action_attempts=2,
            max_elapsed_ms=120_000,
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        ),
    )


def _replay_and_grade(
    *,
    case,
    trace: AgentTrace,
    trace_store: TraceStore,
    base: Path,
    task_authority_before,
    authority_root: Path,
    handler_ledger_counter,
    protected_profile_reads,
):
    before_counter = tuple(handler_ledger_counter())
    before_files = _file_snapshot(base)
    replayed = TraceReplayer(trace_store).replay_trace(trace.trace_id)
    after_counter = tuple(handler_ledger_counter())
    after_files = _file_snapshot(base)
    clean_exact = replayed == trace and replayed.terminal == trace.terminal
    side_effect_free = before_counter == after_counter and before_files == after_files
    tamper_error_code = None
    if case.trace_mutation == "observation_payload_without_rehash":
        tamper_error_code = _tamper_and_replay(case.task_id, trace, base)
    authority_after = _authority_snapshot(authority_root)
    strategy_writes = _authority_change_count(
        task_authority_before,
        authority_after,
    )
    measured_protected_reads = protected_profile_reads()
    forbidden_absent = (
        _forbidden_effects_absent(base)
        and strategy_writes == 0
        and measured_protected_reads == 0
    )
    return grade_task(
        case=case,
        trace=trace,
        clean_replay_exact=clean_exact,
        replay_side_effect_free=side_effect_free,
        tamper_error_code=tamper_error_code,
        forbidden_effects_absent=forbidden_absent,
        handler_ledger=after_counter,
        protected_profile_reads=measured_protected_reads,
        strategy_writes=strategy_writes,
    )


def _tamper_and_replay(task_id: str, trace: AgentTrace, base: Path) -> str:
    store = TraceStore(base / "tampered-traces" / task_id)
    payload = trace.model_dump(mode="json")
    event = next(
        item for item in payload["events"] if item["event_type"] == "tool_observed"
    )
    event["observation"]["payload"]["query_count"] += 1
    path = store.root / f"trace-{trace.trace_id}.json"
    write_immutable_json(path, payload)
    try:
        TraceReplayer(store).replay_trace(trace.trace_id)
    except AgentReplayError as exc:
        return exc.code
    except (TypeError, ValueError):
        return "trace_replay_failed"
    return "tampered_trace_accepted"


def _forbidden_effects_absent(root: Path) -> bool:
    return not any(path.name in _FORBIDDEN_NAMES for path in root.rglob("*"))


def _authority_snapshot(root: Path) -> tuple[tuple[str, str, int], ...]:
    """Hash the actual strategy authority paths without following symlinks."""

    records: list[tuple[str, str, int]] = []
    for name in sorted(_FORBIDDEN_NAMES):
        authority = root / name
        if authority.is_symlink():
            records.append((name, "symlink", authority.lstat().st_size))
            continue
        if not authority.exists():
            records.append((name, "absent", 0))
            continue
        candidates = (authority,) if authority.is_file() else authority.rglob("*")
        for candidate in candidates:
            relative = str(candidate.relative_to(root))
            if candidate.is_symlink():
                records.append((relative, "symlink", candidate.lstat().st_size))
            elif candidate.is_file():
                records.append(
                    (relative, _file_sha256(candidate), candidate.stat().st_size)
                )
            elif candidate.is_dir():
                records.append((relative, "directory", 0))
    return tuple(sorted(records))


def _authority_change_count(
    before: tuple[tuple[str, str, int], ...],
    after: tuple[tuple[str, str, int], ...],
) -> int:
    before_by_path = {path: (digest, size) for path, digest, size in before}
    after_by_path = {path: (digest, size) for path, digest, size in after}
    return sum(
        before_by_path.get(path) != after_by_path.get(path)
        for path in before_by_path.keys() | after_by_path.keys()
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_artifact_capacity(root: Path) -> None:
    observed = sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if observed > MAX_AGENT_EVAL_STORE_BYTES:
        raise RuntimeError("Agent Eval artifact store exceeds its size limit")


def _file_snapshot(root: Path) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(
            (str(path.relative_to(root)), path.stat().st_size)
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
