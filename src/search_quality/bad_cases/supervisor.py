"""Killable POSIX subprocess boundary for Bad Case diagnostics.

The supervisor is intentionally independent of FastAPI and the CLI. Both entry
points can call the same function without relying on thread cancellation.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from search_quality.data.contracts import canonical_json_sha256
from search_quality.evaluation.artifacts import write_immutable_json
from search_quality.observability import new_trace_id, normalize_trace_id

from .artifacts import (
    MAX_EVIDENCE_BYTES,
    MAX_RECEIPT_BYTES,
    BadCaseRunInProgress,
    ensure_bad_case_capacity,
    trusted_bad_case_root,
)
from .contracts import (
    BadCaseDiagnosticArtifact,
    BadCaseExecutionReceipt,
    BadCaseFailedAttempt,
    BadCaseRun,
)
from .worker_contracts import (
    EXECUTION_ID_PATTERN,
    BadCaseSupervisorExecutionReceipt,
    BadCaseWorkerAttempt,
    BadCaseWorkerCompleted,
    BadCaseWorkerFailed,
    BadCaseWorkerRequest,
    supervisor_execution_receipt_id,
)

logger = logging.getLogger("search_quality.bad_case_supervisor")

DEFAULT_WORKER_DEADLINE_MS = 125_000
DEFAULT_TERM_GRACE_MS = 1_000
DEFAULT_KILL_GRACE_MS = 1_000
MAX_WORKER_ENVELOPE_BYTES = 256 * 1024
MAX_SUPERVISOR_RECEIPT_BYTES = MAX_RECEIPT_BYTES
SUPERVISOR_POLICY_ID = "posix-process-group-deadline-v1"
_FRAME_HEADER_BYTES = 4
_READ_CHUNK_BYTES = 64 * 1024
_POLL_INTERVAL_SECONDS = 0.02
_WORKER_MODULE = "search_quality.bad_cases.worker"
_SAFE_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"})
CompletionObservation = Literal[
    "worker_result",
    "deadline_boundary_recovery",
    "protocol_recovery",
]


class BadCaseWorkerError(RuntimeError):
    """Base class carrying only safe worker correlation metadata."""

    def __init__(self, message: str, *, execution_id: str, error_code: str) -> None:
        super().__init__(message)
        self.execution_id = execution_id
        self.error_code = error_code


class BadCaseWorkerDeadlineExceeded(BadCaseWorkerError):
    """The isolated worker crossed its monotonic wall-clock deadline."""

    completed_query_count = None
    count_semantics = "unknown"


class BadCaseWorkerProtocolError(BadCaseWorkerError):
    """The child exited without one valid bounded envelope."""


class BadCaseWorkerProcessFailed(BadCaseWorkerError):
    """The child reported or suffered a safe operational failure."""


class BadCaseWorkerUnreaped(BadCaseWorkerError):
    """SIGKILL was sent but the process group could not be confirmed gone."""


class _WorkerInterrupted(RuntimeError):
    def __init__(
        self,
        *,
        returncode: int | None,
        termination_signal: str | None,
        kill_escalated: bool,
        process_group_alive: bool,
    ) -> None:
        super().__init__("Bad Case worker supervision was interrupted")
        self.returncode = returncode
        self.termination_signal = termination_signal
        self.kill_escalated = kill_escalated
        self.process_group_alive = process_group_alive


@dataclass(frozen=True, slots=True)
class WorkerProcessResult:
    returncode: int | None
    framed_payload: bytes
    payload_overflow: bool
    timed_out: bool
    termination_signal: str | None
    kill_escalated: bool
    process_group_alive: bool
    duration_ms: float


def supervise_bad_case_diagnostics(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None,
    catalog_index_path: str | Path,
    executor_revision: str,
    source_profile: str = "smoke",
    deadline_ms: int = DEFAULT_WORKER_DEADLINE_MS,
    term_grace_ms: int = DEFAULT_TERM_GRACE_MS,
    kill_grace_ms: int = DEFAULT_KILL_GRACE_MS,
    trace_id: str | None = None,
) -> BadCaseRun:
    """Run one fixed worker and force-terminate its POSIX process group."""

    _require_supported_platform()
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Bad Case project root must be a directory")
    run_root = root / "runs" if artifact_root is None else Path(artifact_root)
    if not run_root.is_absolute():
        raise ValueError("Bad Case artifact root must be absolute")
    index = Path(catalog_index_path)
    if index.is_symlink():
        raise ValueError("catalog index must be a regular non-symlink file")
    index = index.resolve(strict=True)
    if not index.is_file():
        raise ValueError("catalog index must be a regular non-symlink file")

    execution_id = f"bad-case-execution-{new_trace_id()}"
    started_at_utc = datetime.now(UTC)
    started = time.monotonic()
    request = BadCaseWorkerRequest(
        execution_id=execution_id,
        trace_id=normalize_trace_id(trace_id),
        execution_started_at_utc=started_at_utc,
        project_root=str(root),
        artifact_root=str(run_root),
        catalog_index_path=str(index),
        executor_revision=executor_revision,
        source_profile=source_profile,
        deadline_ms=deadline_ms,
    )
    _validate_grace_period(term_grace_ms, label="TERM")
    _validate_grace_period(kill_grace_ms, label="KILL")

    with bad_case_supervisor_lock(run_root):
        environment = _build_worker_environment(request)
        command = (sys.executable, "-I", "-m", _WORKER_MODULE)
        logger.info(
            "bad_case_worker_dispatch_started",
            extra={
                "deadline_ms": deadline_ms,
                "execution_id": execution_id,
                "trace_id": request.trace_id,
            },
        )
        try:
            process = _execute_worker_process(
                command=command,
                environment=environment,
                deadline_ms=deadline_ms,
                term_grace_ms=term_grace_ms,
                kill_grace_ms=kill_grace_ms,
            )
        except _WorkerInterrupted as exc:
            _store_supervisor_attempt_if_no_terminal(
                run_root=run_root,
                request=request,
                status="cancelled",
                failure_stage=(
                    "worker_reap" if exc.process_group_alive else "worker_process"
                ),
                error_code=(
                    "worker_unreaped" if exc.process_group_alive else "worker_cancelled"
                ),
                completed_query_count=None,
                count_semantics="unknown",
                termination_signal=exc.termination_signal,
                kill_escalated=exc.kill_escalated,
                worker_exit_code=exc.returncode,
                duration_ms=_elapsed_ms(started),
            )
            raise KeyboardInterrupt from exc
        except OSError as exc:
            _store_supervisor_attempt(
                run_root=run_root,
                request=request,
                status="failed",
                failure_stage="worker_startup",
                error_code="worker_start_failed",
                completed_query_count=0,
                count_semantics="exact",
                termination_signal=None,
                kill_escalated=False,
                worker_exit_code=None,
                duration_ms=_elapsed_ms(started),
            )
            logger.error(
                "bad_case_worker_start_failed",
                extra={
                    "error_type": type(exc).__name__,
                    "execution_id": execution_id,
                },
            )
            raise BadCaseWorkerProcessFailed(
                "Bad Case worker could not start",
                execution_id=execution_id,
                error_code="worker_start_failed",
            ) from exc

        recovered = _recover_completed_run(
            run_root=run_root,
            execution_id=execution_id,
        )
        if process.process_group_alive:
            _store_supervisor_attempt_if_no_terminal(
                run_root=run_root,
                request=request,
                status="failed",
                failure_stage="worker_reap",
                error_code="worker_unreaped",
                completed_query_count=None,
                count_semantics="unknown",
                termination_signal=process.termination_signal,
                kill_escalated=process.kill_escalated,
                worker_exit_code=process.returncode,
                duration_ms=process.duration_ms,
            )
            logger.critical(
                "bad_case_worker_unreaped",
                extra={
                    "execution_id": execution_id,
                    "worker_exit_code": process.returncode,
                },
            )
            raise BadCaseWorkerUnreaped(
                "Bad Case worker process group could not be reaped",
                execution_id=execution_id,
                error_code="worker_unreaped",
            )

        if process.timed_out:
            if recovered is not None:
                logger.warning(
                    "bad_case_worker_completed_at_deadline_boundary",
                    extra={"execution_id": execution_id},
                )
                return _complete_supervision(
                    run_root=run_root,
                    request=request,
                    run=recovered,
                    term_grace_ms=term_grace_ms,
                    kill_grace_ms=kill_grace_ms,
                    completion_observation="deadline_boundary_recovery",
                )
            _store_supervisor_attempt_if_no_terminal(
                run_root=run_root,
                request=request,
                status="timed_out",
                failure_stage="worker_deadline",
                error_code="worker_deadline_exceeded",
                completed_query_count=None,
                count_semantics="unknown",
                termination_signal=process.termination_signal,
                kill_escalated=process.kill_escalated,
                worker_exit_code=process.returncode,
                duration_ms=process.duration_ms,
            )
            logger.error(
                "bad_case_worker_deadline_exceeded",
                extra={
                    "deadline_ms": deadline_ms,
                    "execution_id": execution_id,
                    "kill_escalated": process.kill_escalated,
                    "termination_signal": process.termination_signal,
                },
            )
            raise BadCaseWorkerDeadlineExceeded(
                "Bad Case worker deadline exceeded",
                execution_id=execution_id,
                error_code="worker_deadline_exceeded",
            )

        try:
            envelope = _decode_worker_envelope(
                process.framed_payload,
                overflow=process.payload_overflow,
            )
        except (UnicodeDecodeError, ValueError, ValidationError) as exc:
            if recovered is not None:
                logger.warning(
                    "bad_case_worker_result_recovered",
                    extra={"execution_id": execution_id},
                )
                return _complete_supervision(
                    run_root=run_root,
                    request=request,
                    run=recovered,
                    term_grace_ms=term_grace_ms,
                    kill_grace_ms=kill_grace_ms,
                    completion_observation="protocol_recovery",
                )
            _store_supervisor_attempt_if_no_terminal(
                run_root=run_root,
                request=request,
                status="failed",
                failure_stage="worker_protocol",
                error_code="worker_protocol_invalid",
                completed_query_count=None,
                count_semantics="unknown",
                termination_signal=process.termination_signal,
                kill_escalated=process.kill_escalated,
                worker_exit_code=process.returncode,
                duration_ms=process.duration_ms,
            )
            logger.error(
                "bad_case_worker_protocol_invalid",
                extra={
                    "error_type": type(exc).__name__,
                    "execution_id": execution_id,
                    "worker_exit_code": process.returncode,
                },
            )
            raise BadCaseWorkerProtocolError(
                "Bad Case worker protocol is invalid",
                execution_id=execution_id,
                error_code="worker_protocol_invalid",
            ) from exc

        if envelope.execution_id != execution_id:
            if recovered is not None:
                logger.warning(
                    "bad_case_worker_result_recovered",
                    extra={"execution_id": execution_id},
                )
                return _complete_supervision(
                    run_root=run_root,
                    request=request,
                    run=recovered,
                    term_grace_ms=term_grace_ms,
                    kill_grace_ms=kill_grace_ms,
                    completion_observation="protocol_recovery",
                )
            _store_supervisor_attempt_if_no_terminal(
                run_root=run_root,
                request=request,
                status="failed",
                failure_stage="worker_protocol",
                error_code="worker_protocol_invalid",
                completed_query_count=None,
                count_semantics="unknown",
                termination_signal=process.termination_signal,
                kill_escalated=process.kill_escalated,
                worker_exit_code=process.returncode,
                duration_ms=process.duration_ms,
            )
            raise BadCaseWorkerProtocolError(
                "Bad Case worker execution linkage is invalid",
                execution_id=execution_id,
                error_code="worker_protocol_invalid",
            )
        if isinstance(envelope, BadCaseWorkerCompleted):
            if process.returncode != 0:
                if recovered is not None:
                    logger.warning(
                        "bad_case_worker_result_recovered",
                        extra={"execution_id": execution_id},
                    )
                    return _complete_supervision(
                        run_root=run_root,
                        request=request,
                        run=recovered,
                        term_grace_ms=term_grace_ms,
                        kill_grace_ms=kill_grace_ms,
                        completion_observation="protocol_recovery",
                    )
                _store_supervisor_attempt_if_no_terminal(
                    run_root=run_root,
                    request=request,
                    status="failed",
                    failure_stage="worker_protocol",
                    error_code="worker_protocol_invalid",
                    completed_query_count=None,
                    count_semantics="unknown",
                    termination_signal=process.termination_signal,
                    kill_escalated=process.kill_escalated,
                    worker_exit_code=process.returncode,
                    duration_ms=process.duration_ms,
                )
                raise BadCaseWorkerProtocolError(
                    "Bad Case worker completion exit status is invalid",
                    execution_id=execution_id,
                    error_code="worker_protocol_invalid",
                )
            _validate_run_paths(envelope.run, run_root=run_root)
            logger.info(
                "bad_case_worker_dispatch_completed",
                extra={
                    "diagnostic_id": envelope.run.artifact.diagnostic_id,
                    "duration_ms": round(process.duration_ms, 3),
                    "execution_id": execution_id,
                },
            )
            return _complete_supervision(
                run_root=run_root,
                request=request,
                run=envelope.run,
                term_grace_ms=term_grace_ms,
                kill_grace_ms=kill_grace_ms,
                completion_observation="worker_result",
            )
        if recovered is not None:
            logger.warning(
                "bad_case_worker_result_recovered",
                extra={"execution_id": execution_id},
            )
            return _complete_supervision(
                run_root=run_root,
                request=request,
                run=recovered,
                term_grace_ms=term_grace_ms,
                kill_grace_ms=kill_grace_ms,
                completion_observation="protocol_recovery",
            )
        if envelope.status == "in_progress":
            raise BadCaseRunInProgress("Bad Case diagnostics are already running")
        _store_supervisor_attempt_if_no_terminal(
            run_root=run_root,
            request=request,
            status="failed",
            failure_stage="worker_process",
            error_code=envelope.error_code,
            completed_query_count=None,
            count_semantics="unknown",
            termination_signal=process.termination_signal,
            kill_escalated=process.kill_escalated,
            worker_exit_code=process.returncode,
            duration_ms=process.duration_ms,
        )
        raise BadCaseWorkerProcessFailed(
            "Bad Case worker reported an operational failure",
            execution_id=execution_id,
            error_code=envelope.error_code,
        )


@contextmanager
def bad_case_supervisor_lock(artifact_root: str | Path) -> Iterator[Path]:
    """Serialize API/CLI supervisors without self-locking their child worker."""

    base = trusted_bad_case_root(artifact_root)
    lock_path = base / ".supervisor.lock"
    if lock_path.is_symlink():
        raise ValueError("Bad Case supervisor lock must not be a symbolic link")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Bad Case supervisor lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BadCaseRunInProgress(
                "Bad Case diagnostics are already running"
            ) from exc
        yield base
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _execute_worker_process(
    *,
    command: Sequence[str],
    environment: Mapping[str, str],
    deadline_ms: int,
    term_grace_ms: int,
    kill_grace_ms: int,
) -> WorkerProcessResult:
    """Execute a fixed argv and drain one bounded anonymous result frame."""

    _require_supported_platform()
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("Bad Case worker command is invalid")
    _validate_deadline(deadline_ms)
    _validate_grace_period(term_grace_ms, label="TERM")
    _validate_grace_period(kill_grace_ms, label="KILL")
    result_read, result_write = os.pipe()
    parent_read, parent_write = os.pipe()
    os.set_blocking(result_read, False)
    child_environment = dict(environment)
    child_environment.update(
        {
            "SEARCH_BAD_CASE_RESULT_FD": str(result_write),
            "SEARCH_BAD_CASE_PARENT_FD": str(parent_read),
        }
    )
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            close_fds=True,
            pass_fds=(result_write, parent_read),
            start_new_session=True,
            env=child_environment,
        )
    except BaseException:
        _close_fds(result_read, result_write, parent_read, parent_write)
        raise
    os.close(result_write)
    os.close(parent_read)
    process_group_id = process.pid
    payload = bytearray()
    overflow = False
    timed_out = False
    termination_signal: str | None = None
    kill_escalated = False
    process_group_alive = False
    selector = selectors.DefaultSelector()
    selector.register(result_read, selectors.EVENT_READ)
    deadline = started + (deadline_ms / 1_000.0)
    try:
        while process.poll() is None:
            overflow = _drain_result_fd(result_read, payload) or overflow
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            selector.select(timeout=min(_POLL_INTERVAL_SECONDS, remaining))

        if timed_out:
            termination_signal, kill_escalated, process_group_alive = (
                _terminate_process_group(
                    process,
                    process_group_id=process_group_id,
                    term_grace_ms=term_grace_ms,
                    kill_grace_ms=kill_grace_ms,
                )
            )
        elif _process_group_exists(process_group_id):
            # A worker is never allowed to report completion while leaving a
            # model/helper descendant behind.
            termination_signal, kill_escalated, process_group_alive = (
                _terminate_process_group(
                    process,
                    process_group_id=process_group_id,
                    term_grace_ms=term_grace_ms,
                    kill_grace_ms=kill_grace_ms,
                )
            )
        overflow = _drain_result_fd(result_read, payload) or overflow
    except KeyboardInterrupt as exc:
        termination_signal, kill_escalated, process_group_alive = (
            _terminate_process_group(
                process,
                process_group_id=process_group_id,
                term_grace_ms=term_grace_ms,
                kill_grace_ms=kill_grace_ms,
            )
        )
        raise _WorkerInterrupted(
            returncode=process.poll(),
            termination_signal=termination_signal,
            kill_escalated=kill_escalated,
            process_group_alive=process_group_alive,
        ) from exc
    except BaseException:
        with contextlib.suppress(Exception):
            _terminate_process_group(
                process,
                process_group_id=process_group_id,
                term_grace_ms=term_grace_ms,
                kill_grace_ms=kill_grace_ms,
            )
        raise
    finally:
        selector.close()
        _close_fds(result_read, parent_write)

    return WorkerProcessResult(
        returncode=process.poll(),
        framed_payload=bytes(payload),
        payload_overflow=overflow,
        timed_out=timed_out,
        termination_signal=termination_signal,
        kill_escalated=kill_escalated,
        process_group_alive=process_group_alive,
        duration_ms=_elapsed_ms(started),
    )


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    process_group_id: int,
    term_grace_ms: int,
    kill_grace_ms: int,
) -> tuple[str | None, bool, bool]:
    if process.poll() is None:
        try:
            observed_group = os.getpgid(process.pid)
        except ProcessLookupError:
            observed_group = process_group_id
        if observed_group != process_group_id or observed_group != process.pid:
            raise RuntimeError("Bad Case worker process group identity changed")
    if not _process_group_exists(process_group_id):
        process.poll()
        return None, False, False
    _signal_process_group(process_group_id, signal.SIGTERM)
    if _wait_for_group_exit(
        process,
        process_group_id=process_group_id,
        timeout_ms=term_grace_ms,
    ):
        return "SIGTERM", False, False
    _signal_process_group(process_group_id, signal.SIGKILL)
    gone = _wait_for_group_exit(
        process,
        process_group_id=process_group_id,
        timeout_ms=kill_grace_ms,
    )
    return "SIGKILL", True, not gone


def _wait_for_group_exit(
    process: subprocess.Popen[bytes],
    *,
    process_group_id: int,
    timeout_ms: int,
) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1_000.0)
    while True:
        process.poll()
        if not _process_group_exists(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SECONDS)


def _signal_process_group(process_group_id: int, requested: signal.Signals) -> None:
    if process_group_id <= 1:
        raise RuntimeError("Bad Case worker process group is invalid")
    try:
        os.killpg(process_group_id, requested)
    except ProcessLookupError:
        return


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _drain_result_fd(descriptor: int, payload: bytearray) -> bool:
    overflow = False
    while True:
        try:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return overflow
        if not chunk:
            return overflow
        remaining = MAX_WORKER_ENVELOPE_BYTES + _FRAME_HEADER_BYTES + 1 - len(payload)
        if remaining > 0:
            payload.extend(chunk[:remaining])
        if len(chunk) > remaining or len(payload) > (
            MAX_WORKER_ENVELOPE_BYTES + _FRAME_HEADER_BYTES
        ):
            overflow = True


def _decode_worker_envelope(
    framed: bytes,
    *,
    overflow: bool,
) -> BadCaseWorkerCompleted | BadCaseWorkerFailed:
    if overflow or len(framed) < _FRAME_HEADER_BYTES:
        raise ValueError("worker envelope exceeds or lacks its bounded frame")
    expected = struct.unpack(">I", framed[:_FRAME_HEADER_BYTES])[0]
    encoded = framed[_FRAME_HEADER_BYTES:]
    if expected > MAX_WORKER_ENVELOPE_BYTES or len(encoded) != expected:
        raise ValueError("worker envelope frame length is invalid")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("worker envelope contains duplicate JSON keys")
            result[key] = value
        return result

    payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=reject_duplicates)
    if not isinstance(payload, dict):
        raise ValueError("worker envelope must be a JSON object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    if payload.get("status") == "completed":
        return BadCaseWorkerCompleted.model_validate_json(canonical, strict=True)
    return BadCaseWorkerFailed.model_validate_json(canonical, strict=True)


def _build_worker_environment(request: BadCaseWorkerRequest) -> dict[str, str]:
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "SEARCH_BAD_CASE_REQUEST": json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ),
    }
    log_format = os.environ.get("SEARCH_LOG_FORMAT")
    if log_format in {"json", "text"}:
        environment["SEARCH_LOG_FORMAT"] = log_format
    for key in (
        "SEARCH_LOG_LEVEL",
        "SEARCH_LOG_LEVEL_BAD_CASE",
        "SEARCH_LOG_LEVEL_BAD_CASE_WORKER",
    ):
        value = os.environ.get(key)
        if value and value.upper() in _SAFE_LOG_LEVELS:
            environment[key] = value.upper()
    return environment


def _recover_completed_run(
    *,
    run_root: Path,
    execution_id: str,
) -> BadCaseRun | None:
    base = trusted_bad_case_root(run_root)
    execution_path = base / "executions" / f"{execution_id}.json"
    attempt_path = base / "attempts" / f"{execution_id}.json"
    execution_exists = execution_path.exists() or execution_path.is_symlink()
    attempt_exists = attempt_path.exists() or attempt_path.is_symlink()
    if execution_exists and attempt_exists:
        raise BadCaseWorkerProtocolError(
            "Bad Case execution has conflicting terminal receipts",
            execution_id=execution_id,
            error_code="worker_terminal_conflict",
        )
    if not execution_exists:
        return None
    execution_payload = _load_bounded_json(
        execution_path,
        maximum=MAX_RECEIPT_BYTES,
    )
    execution = BadCaseExecutionReceipt.model_validate_json(
        json.dumps(execution_payload, allow_nan=False), strict=True
    )
    if execution.execution_id != execution_id:
        raise ValueError("Bad Case execution receipt linkage is invalid")
    evidence_path = base / "evidence" / f"{execution.diagnostic_id}.json"
    artifact_payload = _load_bounded_json(
        evidence_path,
        maximum=MAX_EVIDENCE_BYTES,
    )
    artifact = BadCaseDiagnosticArtifact.model_validate_json(
        json.dumps(artifact_payload, allow_nan=False), strict=True
    )
    run = BadCaseRun(
        artifact=artifact,
        execution=execution,
        samples=[],
        artifact_path=str(evidence_path),
        execution_path=str(execution_path),
    )
    _validate_run_paths(run, run_root=run_root)
    return run


def load_supervisor_execution_receipt(
    artifact_root: str | Path,
    execution_id: str,
) -> BadCaseSupervisorExecutionReceipt:
    """Safely load one supervisor receipt selected only by a strict execution ID."""

    safe_execution_id = _validated_execution_id(execution_id)
    base = trusted_bad_case_root(artifact_root)
    receipt_dir = _trusted_supervisor_receipt_dir(base, create=False)
    path = receipt_dir / f"{safe_execution_id}.json"
    payload = _load_bounded_json(
        path,
        maximum=MAX_SUPERVISOR_RECEIPT_BYTES,
        require_private=True,
    )
    receipt = BadCaseSupervisorExecutionReceipt.model_validate_json(
        json.dumps(payload, allow_nan=False), strict=True
    )
    if receipt.execution_id != safe_execution_id:
        raise ValueError("Bad Case supervisor receipt linkage is invalid")
    durable_run = _recover_completed_run(
        run_root=Path(artifact_root),
        execution_id=safe_execution_id,
    )
    if durable_run is None:
        raise ValueError("Bad Case supervisor receipt lacks its child execution")
    child_payload = durable_run.execution.model_dump(mode="json")
    if (
        receipt.diagnostic_id != durable_run.artifact.diagnostic_id
        or receipt.child_execution_schema != durable_run.execution.schema_version
        or receipt.child_execution_id != durable_run.execution.execution_id
        or receipt.child_execution_receipt_sha256
        != canonical_json_sha256(child_payload)
    ):
        raise ValueError("Bad Case supervisor receipt child linkage is invalid")
    return receipt


def _complete_supervision(
    *,
    run_root: Path,
    request: BadCaseWorkerRequest,
    run: BadCaseRun,
    term_grace_ms: int,
    kill_grace_ms: int,
    completion_observation: CompletionObservation,
) -> BadCaseRun:
    """Publish the supervisor terminal receipt before exposing completion."""

    _validate_run_paths(run, run_root=run_root)
    durable_run = _recover_completed_run(
        run_root=run_root,
        execution_id=request.execution_id,
    )
    if durable_run is None:
        raise BadCaseWorkerProtocolError(
            "Bad Case worker completion lacks its durable child receipt",
            execution_id=request.execution_id,
            error_code="worker_protocol_invalid",
        )
    if durable_run.artifact != run.artifact or durable_run.execution != run.execution:
        raise BadCaseWorkerProtocolError(
            "Bad Case worker completion conflicts with its durable artifacts",
            execution_id=request.execution_id,
            error_code="worker_terminal_conflict",
        )
    receipt = _store_supervisor_execution_receipt(
        run_root=run_root,
        request=request,
        durable_run=durable_run,
        term_grace_ms=term_grace_ms,
        kill_grace_ms=kill_grace_ms,
        completion_observation=completion_observation,
    )
    logger.info(
        "bad_case_supervisor_receipt_stored",
        extra={
            "diagnostic_id": receipt.diagnostic_id,
            "execution_id": receipt.execution_id,
            "receipt_id": receipt.receipt_id,
        },
    )
    return run


def _store_supervisor_execution_receipt(
    *,
    run_root: Path,
    request: BadCaseWorkerRequest,
    durable_run: BadCaseRun,
    term_grace_ms: int,
    kill_grace_ms: int,
    completion_observation: CompletionObservation,
) -> BadCaseSupervisorExecutionReceipt:
    """Publish one immutable supervisor terminal after revalidating child state."""

    # Re-read both child receipt and evidence immediately before publication.
    verified = _recover_completed_run(
        run_root=run_root,
        execution_id=request.execution_id,
    )
    if verified is None or (
        verified.artifact != durable_run.artifact
        or verified.execution != durable_run.execution
    ):
        raise BadCaseWorkerProtocolError(
            "Bad Case child artifacts changed before supervisor publication",
            execution_id=request.execution_id,
            error_code="worker_terminal_conflict",
        )
    core: dict[str, object] = {
        "schema_version": "bad-case-supervisor-execution-v1",
        "execution_id": request.execution_id,
        "diagnostic_id": verified.artifact.diagnostic_id,
        "child_execution_schema": verified.execution.schema_version,
        "child_execution_id": verified.execution.execution_id,
        "child_execution_receipt_sha256": canonical_json_sha256(
            verified.execution.model_dump(mode="json")
        ),
        "policy_id": SUPERVISOR_POLICY_ID,
        "deadline_ms": request.deadline_ms,
        "term_grace_ms": term_grace_ms,
        "kill_grace_ms": kill_grace_ms,
        "completion_observation": completion_observation,
        "trace_id": request.trace_id,
        "completed": True,
    }
    candidate = BadCaseSupervisorExecutionReceipt.model_validate(
        {
            **core,
            "receipt_id": supervisor_execution_receipt_id(core),
        },
        strict=True,
    )
    base = trusted_bad_case_root(run_root)
    attempt_path = base / "attempts" / f"{request.execution_id}.json"
    if attempt_path.exists() or attempt_path.is_symlink():
        _load_existing_attempt(attempt_path, execution_id=request.execution_id)
        raise BadCaseWorkerProtocolError(
            "Bad Case execution has conflicting terminal receipts",
            execution_id=request.execution_id,
            error_code="worker_terminal_conflict",
        )
    receipt_dir = _trusted_supervisor_receipt_dir(base, create=True)
    path = receipt_dir / f"{request.execution_id}.json"
    if path.exists() or path.is_symlink():
        existing = load_supervisor_execution_receipt(
            run_root,
            request.execution_id,
        )
        if existing != candidate:
            raise BadCaseWorkerProtocolError(
                "Bad Case execution has a conflicting supervisor receipt",
                execution_id=request.execution_id,
                error_code="worker_terminal_conflict",
            )
        return existing
    ensure_bad_case_capacity(base)
    write_immutable_json(path, candidate.model_dump(mode="json"))
    stored = load_supervisor_execution_receipt(run_root, request.execution_id)
    if stored != candidate:
        raise BadCaseWorkerProtocolError(
            "Bad Case supervisor receipt failed durable verification",
            execution_id=request.execution_id,
            error_code="worker_terminal_conflict",
        )
    return stored


def _trusted_supervisor_receipt_dir(base: Path, *, create: bool) -> Path:
    directory = base / "supervisor-executions"
    if directory.is_symlink():
        raise ValueError("Bad Case supervisor receipt directory is a symbolic link")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = directory.resolve(strict=True)
    details = resolved.stat()
    if (
        resolved.parent != base
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise ValueError("Bad Case supervisor receipt directory is not private")
    return resolved


def _validated_execution_id(value: str) -> str:
    if type(value) is not str or re.fullmatch(EXECUTION_ID_PATTERN, value) is None:
        raise ValueError("Bad Case execution ID has an invalid format")
    return value


def _store_supervisor_attempt_if_no_terminal(**kwargs: Any) -> Path | None:
    run_root = Path(kwargs["run_root"])
    request: BadCaseWorkerRequest = kwargs["request"]
    base = trusted_bad_case_root(run_root)
    execution_path = base / "executions" / f"{request.execution_id}.json"
    attempt_path = base / "attempts" / f"{request.execution_id}.json"
    if execution_path.exists() or execution_path.is_symlink():
        return None
    if attempt_path.exists() or attempt_path.is_symlink():
        _load_existing_attempt(attempt_path, execution_id=request.execution_id)
        return attempt_path
    try:
        ensure_bad_case_capacity(base)
    except RuntimeError as exc:
        logger.warning(
            "bad_case_worker_attempt_not_stored",
            extra={
                "error_type": type(exc).__name__,
                "execution_id": request.execution_id,
            },
        )
        return None
    return _store_supervisor_attempt(**kwargs)


def _store_supervisor_attempt(
    *,
    run_root: Path,
    request: BadCaseWorkerRequest,
    status: str,
    failure_stage: str,
    error_code: str,
    completed_query_count: int | None,
    count_semantics: str,
    termination_signal: str | None,
    kill_escalated: bool,
    worker_exit_code: int | None,
    duration_ms: float,
) -> Path:
    base = trusted_bad_case_root(run_root)
    execution_path = base / "executions" / f"{request.execution_id}.json"
    if execution_path.exists() or execution_path.is_symlink():
        raise BadCaseWorkerProtocolError(
            "Bad Case execution already has a completed receipt",
            execution_id=request.execution_id,
            error_code="worker_terminal_conflict",
        )
    attempt = BadCaseWorkerAttempt.model_validate(
        {
            "execution_id": request.execution_id,
            "status": status,
            "failure_stage": failure_stage,
            "completed_query_count": completed_query_count,
            "count_semantics": count_semantics,
            "error_code": error_code,
            "deadline_ms": request.deadline_ms,
            "termination_signal": termination_signal,
            "kill_escalated": kill_escalated,
            "worker_exit_code": worker_exit_code,
            "started_at_utc": request.execution_started_at_utc,
            "completed_at_utc": datetime.now(UTC),
            "duration_ms": duration_ms,
        },
        strict=True,
    )
    attempt_dir = base / "attempts"
    if attempt_dir.is_symlink():
        raise ValueError("Bad Case attempt directory must not be a symbolic link")
    attempt_dir.mkdir(parents=True, exist_ok=True)
    resolved = attempt_dir.resolve(strict=True)
    if resolved.parent != base:
        raise ValueError("Bad Case attempt directory escaped its configured root")
    path = resolved / f"{request.execution_id}.json"
    write_immutable_json(path, attempt.model_dump(mode="json"))
    return path


def _load_existing_attempt(path: Path, *, execution_id: str) -> None:
    payload = _load_bounded_json(path, maximum=MAX_RECEIPT_BYTES)
    schema = payload.get("schema_version")
    if schema == "bad-case-failed-attempt-v1":
        attempt = BadCaseFailedAttempt.model_validate_json(
            json.dumps(payload, allow_nan=False), strict=True
        )
    elif schema == "bad-case-worker-attempt-v1":
        attempt = BadCaseWorkerAttempt.model_validate_json(
            json.dumps(payload, allow_nan=False), strict=True
        )
    else:
        raise ValueError("Bad Case failed attempt schema is invalid")
    if attempt.execution_id != execution_id:
        raise ValueError("Bad Case failed attempt linkage is invalid")


def _load_bounded_json(
    path: Path,
    *,
    maximum: int,
    require_private: bool = False,
) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Bad Case terminal artifact must not be a symbolic link")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size > maximum
            or (
                require_private
                and (
                    details.st_uid != os.geteuid()
                    or stat.S_IMODE(details.st_mode) & 0o077
                )
            )
        ):
            raise ValueError("Bad Case terminal artifact is invalid")
        chunks: list[bytes] = []
        observed = 0
        while observed <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > maximum:
            raise ValueError("Bad Case terminal artifact exceeds its size limit")
    finally:
        os.close(descriptor)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Bad Case terminal artifact has duplicate keys")
            result[key] = value
        return result

    payload = json.loads(encoded, object_pairs_hook=reject_duplicates)
    if not isinstance(payload, dict):
        raise ValueError("Bad Case terminal artifact must be an object")
    return payload


def _validate_run_paths(run: BadCaseRun, *, run_root: Path) -> None:
    base = trusted_bad_case_root(run_root)
    expected_artifact = (
        base / "evidence" / f"{run.artifact.diagnostic_id}.json"
    ).resolve(strict=True)
    expected_execution = (
        base / "executions" / f"{run.execution.execution_id}.json"
    ).resolve(strict=True)
    artifact_path = Path(run.artifact_path)
    execution_path = Path(run.execution_path)
    if artifact_path.is_symlink() or execution_path.is_symlink():
        raise ValueError("Bad Case Run paths must not be symbolic links")
    if (
        artifact_path.resolve(strict=True) != expected_artifact
        or execution_path.resolve(strict=True) != expected_execution
    ):
        raise ValueError("Bad Case Run paths escaped their artifact directories")


def _validate_deadline(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 600_000
    ):
        raise ValueError("Bad Case worker deadline is invalid")


def _validate_grace_period(value: int, *, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 30_000
    ):
        raise ValueError(f"Bad Case {label} grace period is invalid")


def _require_supported_platform() -> None:
    if os.name != "posix" or not hasattr(os, "killpg"):
        raise RuntimeError("Bad Case worker isolation requires POSIX process groups")


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1_000.0


def _close_fds(*descriptors: int) -> None:
    for descriptor in descriptors:
        with contextlib.suppress(OSError):
            os.close(descriptor)
