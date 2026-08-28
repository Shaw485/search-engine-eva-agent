"""Finite, policy-controlled runtime for the smoke search evaluation Agent."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from search_quality.observability import logging_context, new_trace_id

from .contracts import (
    SAFE_ID_PATTERN,
    AgentDecision,
    AgentState,
    FinishDecision,
    RuntimeTask,
    TerminalOutcome,
    TerminalResult,
    ToolAction,
    ToolObservation,
    ensure_json_value,
    validate_evidence_ref,
)
from .errors import AgentPolicyError, AgentToolError
from .grounding import validate_action_scope, validate_finish_grounding
from .planner import Planner, PlannerView
from .registry import AgentToolRegistry
from .reporting import build_terminal_report
from .trace import (
    AgentTrace,
    TraceRecorder,
    TraceStore,
    canonical_json_bytes,
    compute_trace_context_hash,
)

runtime_logger = logging.getLogger("search_quality.agent_runtime")
planner_logger = logging.getLogger("search_quality.agent_model")
tool_logger = logging.getLogger("search_quality.agent_tools")
decision_adapter = TypeAdapter(AgentDecision)
RUN_CREATING_TOOLS = frozenset(
    {"run_ranker", "diagnose_baseline_retrieval", "run_retrieval_candidate"}
)


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    max_steps: int = 8
    max_tool_calls: int = 6
    max_run_creations: int = 3
    max_failures: int = 2
    max_same_action_attempts: int = 2
    max_elapsed_ms: int = 30_000
    max_decision_bytes: int = 64 * 1024
    max_observation_bytes: int = 1024 * 1024
    allowed_capabilities: frozenset[str] = frozenset(
        {
            "read_smoke_run",
            "create_smoke_run",
            "compare_smoke_runs",
            "read_smoke_query_evidence",
        }
    )

    def __post_init__(self) -> None:
        numeric = (
            self.max_steps,
            self.max_tool_calls,
            self.max_run_creations,
            self.max_failures,
            self.max_same_action_attempts,
            self.max_elapsed_ms,
            self.max_decision_bytes,
            self.max_observation_bytes,
        )
        if any(type(value) is not int or value < 1 for value in numeric):
            raise ValueError("Runtime policy limits must be positive integers")
        if (
            not isinstance(self.allowed_capabilities, frozenset)
            or not self.allowed_capabilities
            or any(
                not isinstance(capability, str)
                or re.fullmatch(SAFE_ID_PATTERN, capability) is None
                for capability in self.allowed_capabilities
            )
        ):
            raise ValueError(
                "Runtime policy capabilities must be safe identifiers in a frozenset"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["allowed_capabilities"] = sorted(self.allowed_capabilities)
        return payload


class AgentRuntime:
    runtime_id = "search-agent-runtime-v1"

    def __init__(
        self,
        *,
        planner: Planner,
        tools: AgentToolRegistry,
        trace_store: TraceStore,
        policy: RuntimePolicy | None = None,
    ) -> None:
        planner_id = getattr(planner, "planner_id", None)
        if (
            not isinstance(planner_id, str)
            or re.fullmatch(SAFE_ID_PATTERN, planner_id) is None
        ):
            raise ValueError("Planner ID must be a safe identifier")
        self.planner = planner
        self.tools = tools
        self.trace_store = trace_store
        self.policy = policy or RuntimePolicy()

    def run(self, task: RuntimeTask) -> TerminalResult:
        trace_id = new_trace_id()
        recorder = TraceRecorder(
            trace_id,
            context_sha256=compute_trace_context_hash(
                trace_id=trace_id,
                planner_id=self.planner.planner_id,
                task=task,
                policy=self.policy.to_dict(),
                tool_names=sorted(self.tools.names),
            ),
        )
        observations: list[ToolObservation] = []
        state = AgentState.PLANNING
        steps_used = 0
        tool_calls_used = 0
        run_creations = 0
        failures = 0
        action_attempts: dict[str, int] = {}
        started = time.perf_counter()

        with logging_context(
            trace_id=trace_id,
            operation="search_evaluation_agent",
            task_id=task.task_id,
        ):
            runtime_logger.info(
                "agent_run_started",
                extra={"planner_id": self.planner.planner_id},
            )
            while steps_used < self.policy.max_steps:
                if self._elapsed_ms(started) > self.policy.max_elapsed_ms:
                    return self._fail(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                        reason_code="elapsed_budget_exhausted",
                    )
                view = PlannerView(
                    task=task,
                    state=state,
                    observations=tuple(
                        ToolObservation.model_validate(item.model_dump(mode="json"))
                        for item in observations
                    ),
                    steps_used=steps_used,
                    tool_calls_used=tool_calls_used,
                )
                planner_logger.debug(
                    "agent_planner_started",
                    extra={"state": state.value, "step": steps_used + 1},
                )
                try:
                    raw_decision = self.planner.decide(view)
                except Exception:
                    planner_logger.error(
                        "agent_planner_failed",
                        extra={"error_code": "planner_failed", "step": steps_used + 1},
                    )
                    return self._fail(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                        reason_code="planner_failed",
                    )
                try:
                    decision = decision_adapter.validate_python(
                        raw_decision, strict=True
                    )
                    decision_payload = decision.model_dump(mode="python")
                    ensure_json_value(decision_payload)
                    if (
                        len(canonical_json_bytes(decision_payload))
                        > self.policy.max_decision_bytes
                    ):
                        raise ValueError("Planner decision exceeds its size budget")
                except (TypeError, ValueError, ValidationError):
                    planner_logger.error(
                        "agent_planner_failed",
                        extra={
                            "error_code": "planner_invalid_output",
                            "step": steps_used + 1,
                        },
                    )
                    return self._fail(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                        reason_code="planner_invalid_output",
                    )
                planner_logger.debug(
                    "agent_planner_completed",
                    extra={"decision_kind": decision.kind, "step": steps_used + 1},
                )
                steps_used += 1

                if isinstance(decision, FinishDecision):
                    try:
                        validate_finish_grounding(task, decision, observations)
                    except (TypeError, ValueError, AgentPolicyError):
                        return self._fail(
                            task=task,
                            recorder=recorder,
                            observations=observations,
                            state=state,
                            steps_used=steps_used,
                            tool_calls_used=tool_calls_used,
                            reason_code="ungrounded_finish_rejected",
                        )
                    return self._complete(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        decision=decision,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                    )

                if tool_calls_used >= self.policy.max_tool_calls:
                    return self._fail(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                        reason_code="tool_call_budget_exhausted",
                    )
                if decision.tool_name in RUN_CREATING_TOOLS:
                    if run_creations >= self.policy.max_run_creations:
                        return self._fail(
                            task=task,
                            recorder=recorder,
                            observations=observations,
                            state=state,
                            steps_used=steps_used,
                            tool_calls_used=tool_calls_used,
                            reason_code="run_creation_budget_exhausted",
                        )
                    run_creations += 1
                action_key = hashlib.sha256(
                    canonical_json_bytes(decision.model_dump(mode="json"))
                ).hexdigest()
                attempts = action_attempts.get(action_key, 0) + 1
                action_attempts[action_key] = attempts
                if attempts > self.policy.max_same_action_attempts:
                    return self._fail(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                        reason_code="repeated_action_rejected",
                    )

                recorder.append(
                    state_before=state,
                    event_type="action_selected",
                    state_after=AgentState.ACTING,
                    decision=decision,
                )
                state = AgentState.ACTING
                tool_calls_used += 1
                tool_started = time.perf_counter()
                logged_tool_name = (
                    decision.tool_name
                    if decision.tool_name in self.tools.names
                    else "unrecognized"
                )
                tool_logger.debug(
                    "agent_tool_started",
                    extra={"step": steps_used, "tool_name": logged_tool_name},
                )
                observation, policy_violation = self._execute_tool(
                    task,
                    decision,
                    observations=tuple(observations),
                )
                duration_ms = self._elapsed_ms(tool_started)
                tool_logger.log(
                    logging.DEBUG
                    if observation.status == "succeeded"
                    else logging.ERROR,
                    (
                        "agent_tool_completed"
                        if observation.status == "succeeded"
                        else "agent_tool_failed"
                    ),
                    extra={
                        "duration_ms": round(duration_ms, 3),
                        "error_code": observation.error_code,
                        "step": steps_used,
                        "tool_name": logged_tool_name,
                    },
                )
                recorder.append(
                    state_before=state,
                    event_type="tool_observed",
                    state_after=AgentState.OBSERVING,
                    observation=observation,
                    duration_ms=duration_ms,
                )
                observations.append(observation)
                state = AgentState.OBSERVING
                recorder.append(
                    state_before=state,
                    event_type="observation_processed",
                    state_after=AgentState.DECIDING,
                )
                state = AgentState.DECIDING
                if observation.status == "failed":
                    failures += 1
                if policy_violation:
                    return self._fail(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                        reason_code="policy_violation",
                    )
                if failures >= self.policy.max_failures:
                    return self._fail(
                        task=task,
                        recorder=recorder,
                        observations=observations,
                        state=state,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                        reason_code="failure_budget_exhausted",
                    )

            return self._fail(
                task=task,
                recorder=recorder,
                observations=observations,
                state=state,
                steps_used=steps_used,
                tool_calls_used=tool_calls_used,
                reason_code="step_budget_exhausted",
            )

    def _execute_tool(
        self,
        task: RuntimeTask,
        decision: ToolAction,
        *,
        observations: tuple[ToolObservation, ...],
    ) -> tuple[ToolObservation, bool]:
        evidence_ref: str | None = None
        payload: dict[str, Any] = {}
        error_code: str | None = None
        retryable = False
        policy_violation = False
        try:
            validate_action_scope(task, decision, observations)
            result = self.tools.execute(
                decision.tool_name,
                decision.arguments,
                allowed_capabilities=self.policy.allowed_capabilities,
            )
            if not isinstance(result, dict) or set(result) != {
                "evidence_ref",
                "payload",
            }:
                raise AgentToolError("invalid_tool_result")
            try:
                evidence_ref = validate_evidence_ref(result["evidence_ref"])
                payload = result["payload"]
                if not isinstance(payload, dict):
                    raise ValueError("tool payload must be an object")
                serialized = canonical_json_bytes(payload)
            except (TypeError, ValueError) as exc:
                raise AgentToolError("invalid_tool_result") from exc
            if len(serialized) > self.policy.max_observation_bytes:
                raise AgentToolError("observation_too_large")
            status = "succeeded"
        except AgentPolicyError as exc:
            status = "failed"
            error_code = exc.code
            policy_violation = True
            evidence_ref = None
            payload = {}
        except AgentToolError as exc:
            status = "failed"
            error_code = exc.code
            retryable = exc.retryable
            evidence_ref = None
            payload = {}
        except Exception:
            status = "failed"
            error_code = "internal_error"
            evidence_ref = None
            payload = {}
        base = {
            "error_code": error_code,
            "evidence_ref": evidence_ref,
            "payload": payload,
            "retryable": retryable,
            "status": status,
            "tool_name": decision.tool_name,
        }
        ensure_json_value(base)
        sha256 = hashlib.sha256(canonical_json_bytes(base)).hexdigest()
        return ToolObservation(**base, sha256=sha256), policy_violation

    def _complete(
        self,
        *,
        task: RuntimeTask,
        recorder: TraceRecorder,
        observations: list[ToolObservation],
        state: AgentState,
        decision: FinishDecision,
        steps_used: int,
        tool_calls_used: int,
    ) -> TerminalResult:
        result = TerminalResult(
            trace_id=recorder.trace_id,
            state="completed",
            outcome=decision.outcome,
            evidence_refs=decision.evidence_refs,
            reason_code=decision.reason_code,
            report=build_terminal_report(
                task=task,
                outcome=decision.outcome,
                reason_code=decision.reason_code,
                observations=tuple(observations),
                evidence_refs=decision.evidence_refs,
            ),
            steps_used=steps_used,
            tool_calls_used=tool_calls_used,
        )
        recorder.append(
            state_before=state,
            event_type="run_completed",
            state_after=AgentState.COMPLETED,
            decision=decision,
            terminal=result,
        )
        self._store_trace(task=task, recorder=recorder, result=result)
        runtime_logger.info(
            "agent_run_completed",
            extra={
                "outcome": result.outcome.value,
                "steps_used": steps_used,
                "tool_calls_used": tool_calls_used,
            },
        )
        return result

    def _fail(
        self,
        *,
        task: RuntimeTask,
        recorder: TraceRecorder,
        observations: list[ToolObservation],
        state: AgentState,
        steps_used: int,
        tool_calls_used: int,
        reason_code: str,
    ) -> TerminalResult:
        evidence_refs = [
            item.evidence_ref
            for item in observations
            if item.status == "succeeded" and item.evidence_ref is not None
        ]
        result = TerminalResult(
            trace_id=recorder.trace_id,
            state="failed",
            outcome=TerminalOutcome.INCONCLUSIVE,
            evidence_refs=evidence_refs,
            reason_code=reason_code,
            report=build_terminal_report(
                task=task,
                outcome=TerminalOutcome.INCONCLUSIVE,
                reason_code=reason_code,
                observations=tuple(observations),
                evidence_refs=evidence_refs,
            ),
            steps_used=steps_used,
            tool_calls_used=tool_calls_used,
        )
        recorder.append(
            state_before=state,
            event_type="run_failed",
            state_after=AgentState.FAILED,
            terminal=result,
        )
        self._store_trace(task=task, recorder=recorder, result=result)
        runtime_logger.error(
            "agent_run_failed",
            extra={
                "error_code": reason_code,
                "steps_used": steps_used,
                "tool_calls_used": tool_calls_used,
            },
        )
        return result

    def _store_trace(
        self, *, task: RuntimeTask, recorder: TraceRecorder, result: TerminalResult
    ) -> None:
        trace = AgentTrace(
            trace_id=recorder.trace_id,
            planner_id=self.planner.planner_id,
            task=task,
            policy=self.policy.to_dict(),
            tool_names=sorted(self.tools.names),
            events=list(recorder.events),
            terminal=result,
        )
        self.trace_store.store(trace)

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000
