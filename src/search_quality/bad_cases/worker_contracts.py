"""Strict contracts for the isolated Bad Case worker boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from search_quality.agent.contracts import StrictModel
from search_quality.data.contracts import canonical_json_sha256

from .contracts import BadCaseRun

EXECUTION_ID_PATTERN = r"^bad-case-execution-[0-9a-f]{32}$"
TRACE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
REVISION_PATTERN = r"^[0-9a-f]{40}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SUPERVISOR_RECEIPT_ID_PATTERN = r"^bad-case-supervisor-execution-[0-9a-f]{12}$"
WorkerFailureCode = Literal[
    "bad_case_run_in_progress",
    "input_file_missing",
    "permission_denied",
    "invalid_input",
    "runtime_guard_failed",
    "io_failure",
    "internal_error",
]
SupervisorFailureCode = Literal[
    "worker_cancelled",
    "worker_unreaped",
    "worker_start_failed",
    "worker_deadline_exceeded",
    "worker_protocol_invalid",
    "input_file_missing",
    "permission_denied",
    "invalid_input",
    "runtime_guard_failed",
    "io_failure",
    "internal_error",
]


class BadCaseWorkerRequest(StrictModel):
    """Validated, source-bounded configuration passed through an allowlisted env."""

    schema_version: Literal["bad-case-worker-request-v1"] = "bad-case-worker-request-v1"
    execution_id: StrictStr = Field(pattern=EXECUTION_ID_PATTERN)
    trace_id: StrictStr = Field(pattern=TRACE_ID_PATTERN)
    execution_started_at_utc: AwareDatetime
    project_root: StrictStr = Field(min_length=1, max_length=4096)
    artifact_root: StrictStr = Field(min_length=1, max_length=4096)
    catalog_index_path: StrictStr = Field(min_length=1, max_length=4096)
    executor_revision: StrictStr = Field(pattern=REVISION_PATTERN)
    source_profile: Literal["smoke"] = "smoke"
    deadline_ms: StrictInt = Field(ge=1, le=600_000)

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        for value in (
            self.project_root,
            self.artifact_root,
            self.catalog_index_path,
        ):
            if not Path(value).is_absolute():
                raise ValueError("Bad Case worker paths must be absolute")
        return self


class BadCaseWorkerCompleted(StrictModel):
    """One validated completed Run sent over the bounded anonymous pipe."""

    schema_version: Literal["bad-case-worker-envelope-v1"] = (
        "bad-case-worker-envelope-v1"
    )
    status: Literal["completed"] = "completed"
    execution_id: StrictStr = Field(pattern=EXECUTION_ID_PATTERN)
    run: BadCaseRun

    @model_validator(mode="after")
    def validate_execution_link(self) -> Self:
        if self.execution_id != self.run.execution.execution_id:
            raise ValueError("worker envelope execution ID does not match its Run")
        return self


class BadCaseWorkerFailed(StrictModel):
    """Safe failure metadata; exception messages never cross the worker boundary."""

    schema_version: Literal["bad-case-worker-envelope-v1"] = (
        "bad-case-worker-envelope-v1"
    )
    status: Literal["failed", "in_progress"]
    execution_id: StrictStr = Field(pattern=EXECUTION_ID_PATTERN)
    error_code: WorkerFailureCode

    @model_validator(mode="after")
    def validate_status_code(self) -> Self:
        if (self.status == "in_progress") != (
            self.error_code == "bad_case_run_in_progress"
        ):
            raise ValueError("worker failure status and code are inconsistent")
        return self


class BadCaseWorkerAttempt(StrictModel):
    """Supervisor-owned terminal receipt for a worker that could not complete."""

    schema_version: Literal["bad-case-worker-attempt-v1"] = "bad-case-worker-attempt-v1"
    execution_id: StrictStr = Field(pattern=EXECUTION_ID_PATTERN)
    status: Literal["failed", "timed_out", "cancelled"]
    failure_stage: Literal[
        "worker_startup",
        "worker_deadline",
        "worker_protocol",
        "worker_process",
        "worker_reap",
    ]
    completed_query_count: StrictInt | None = Field(default=None, ge=0, le=59)
    count_semantics: Literal["exact", "unknown"]
    error_code: SupervisorFailureCode
    deadline_ms: StrictInt = Field(ge=1, le=600_000)
    termination_signal: Literal["SIGTERM", "SIGKILL"] | None = None
    kill_escalated: StrictBool
    worker_exit_code: StrictInt | None = None
    started_at_utc: AwareDatetime
    completed_at_utc: AwareDatetime
    duration_ms: float = Field(strict=True, ge=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        if self.completed_at_utc < self.started_at_utc:
            raise ValueError("worker attempt completed before it started")
        if (self.completed_query_count is None) != (self.count_semantics == "unknown"):
            raise ValueError("worker attempt count semantics are inconsistent")
        if self.kill_escalated != (self.termination_signal == "SIGKILL"):
            raise ValueError("worker kill escalation does not match its signal")
        if (self.status == "timed_out") != (self.failure_stage == "worker_deadline"):
            raise ValueError("worker deadline stage and status are inconsistent")
        if self.failure_stage == "worker_startup" and (
            self.completed_query_count != 0
            or self.count_semantics != "exact"
            or self.termination_signal is not None
        ):
            raise ValueError("worker startup attempt must prove exact zero work")
        return self


class BadCaseSupervisorExecutionReceipt(StrictModel):
    """Immutable proof that a completed child ran behind the POSIX deadline."""

    schema_version: Literal["bad-case-supervisor-execution-v1"] = (
        "bad-case-supervisor-execution-v1"
    )
    receipt_id: StrictStr = Field(pattern=SUPERVISOR_RECEIPT_ID_PATTERN)
    execution_id: StrictStr = Field(pattern=EXECUTION_ID_PATTERN)
    diagnostic_id: StrictStr = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    child_execution_schema: Literal["bad-case-execution-v1"] = "bad-case-execution-v1"
    child_execution_id: StrictStr = Field(pattern=EXECUTION_ID_PATTERN)
    child_execution_receipt_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    policy_id: Literal["posix-process-group-deadline-v1"] = (
        "posix-process-group-deadline-v1"
    )
    deadline_ms: StrictInt = Field(ge=1, le=600_000)
    term_grace_ms: StrictInt = Field(ge=1, le=30_000)
    kill_grace_ms: StrictInt = Field(ge=1, le=30_000)
    completion_observation: Literal[
        "worker_result",
        "deadline_boundary_recovery",
        "protocol_recovery",
    ]
    trace_id: StrictStr = Field(pattern=TRACE_ID_PATTERN)
    completed: Literal[True] = True

    @model_validator(mode="after")
    def validate_links_and_identity(self) -> Self:
        if self.child_execution_id != self.execution_id:
            raise ValueError("supervisor and child execution IDs do not match")
        expected_id = supervisor_execution_receipt_id(
            self.model_dump(mode="json", exclude={"receipt_id"})
        )
        if self.receipt_id != expected_id:
            raise ValueError("supervisor receipt ID does not match its content")
        return self


def supervisor_execution_receipt_id(payload_without_id: dict[str, object]) -> str:
    """Return the conventional ID whose suffix is the canonical content hash."""

    return (
        "bad-case-supervisor-execution-"
        f"{canonical_json_sha256(payload_without_id)[:12]}"
    )
