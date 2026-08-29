"""Private subprocess entry point for one Bad Case diagnostic execution."""

from __future__ import annotations

import json
import logging
import os
import signal
import struct
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from search_quality.catalog import CatalogSearchService
from search_quality.observability import (
    classify_error,
    configure_logging,
    logging_context,
)

from .artifacts import BadCaseRunInProgress
from .runner import run_bad_case_diagnostics
from .worker_contracts import (
    BadCaseWorkerCompleted,
    BadCaseWorkerFailed,
    BadCaseWorkerRequest,
)

logger = logging.getLogger("search_quality.bad_case_worker")
MAX_REQUEST_BYTES = 32 * 1024
MAX_ENVELOPE_BYTES = 256 * 1024


def main() -> None:
    configure_logging()
    result_fd: int | None = None
    try:
        result_fd = _inherited_fd("SEARCH_BAD_CASE_RESULT_FD")
        parent_fd = _inherited_fd("SEARCH_BAD_CASE_PARENT_FD")
        request = _load_request()
        _start_parent_watchdog(parent_fd)
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        logger.error(
            "bad_case_worker_boot_failed",
            extra={"error_type": type(exc).__name__},
        )
        raise SystemExit(70) from None

    with logging_context(
        trace_id=request.trace_id,
        execution_id=request.execution_id,
        operation="bad_case_isolated_worker",
    ):
        logger.info(
            "bad_case_worker_started",
            extra={
                "deadline_ms": request.deadline_ms,
                "execution_id": request.execution_id,
            },
        )
        try:
            run = run_bad_case_diagnostics(
                project_root=Path(request.project_root),
                artifact_root=Path(request.artifact_root),
                source_profile=request.source_profile,
                revision_provider=lambda _root: request.executor_revision,
                search_service=CatalogSearchService(request.catalog_index_path),
                execution_id=request.execution_id,
                execution_started_at_utc=request.execution_started_at_utc,
            )
            envelope = BadCaseWorkerCompleted(
                execution_id=request.execution_id,
                run=run,
            )
            _write_envelope(result_fd, envelope.model_dump(mode="json"))
        except BadCaseRunInProgress:
            _write_safe_failure(
                result_fd,
                request=request,
                status="in_progress",
                error_code="bad_case_run_in_progress",
            )
            logger.info(
                "bad_case_worker_run_in_progress",
                extra={"execution_id": request.execution_id},
            )
            raise SystemExit(75) from None
        except Exception as exc:
            try:
                _write_safe_failure(
                    result_fd,
                    request=request,
                    status="failed",
                    error_code=classify_error(exc),
                )
            except Exception as envelope_exc:
                logger.error(
                    "bad_case_worker_failure_envelope_failed",
                    extra={"error_type": type(envelope_exc).__name__},
                )
            logger.error(
                "bad_case_worker_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                    "execution_id": request.execution_id,
                },
            )
            raise SystemExit(1) from None
        finally:
            if result_fd is not None:
                try:
                    os.close(result_fd)
                except OSError:
                    pass
        logger.info(
            "bad_case_worker_completed",
            extra={"execution_id": request.execution_id},
        )


def _load_request() -> BadCaseWorkerRequest:
    encoded = os.environ.get("SEARCH_BAD_CASE_REQUEST", "").encode("utf-8")
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("Bad Case worker request is missing or oversized")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Bad Case worker request contains duplicate keys")
            result[key] = value
        return result

    payload = json.loads(encoded, object_pairs_hook=reject_duplicates)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return BadCaseWorkerRequest.model_validate_json(canonical, strict=True)


def _write_safe_failure(
    descriptor: int,
    *,
    request: BadCaseWorkerRequest,
    status: str,
    error_code: str,
) -> None:
    envelope = BadCaseWorkerFailed.model_validate(
        {
            "status": status,
            "execution_id": request.execution_id,
            "error_code": error_code,
        },
        strict=True,
    )
    _write_envelope(descriptor, envelope.model_dump(mode="json"))


def _write_envelope(descriptor: int, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise RuntimeError("Bad Case worker envelope exceeds its size limit")
    framed = struct.pack(">I", len(encoded)) + encoded
    view = memoryview(framed)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Bad Case worker result pipe closed")
        view = view[written:]


def _inherited_fd(key: str) -> int:
    raw = os.environ.get(key)
    if raw is None or not raw.isascii() or not raw.isdigit():
        raise ValueError("Bad Case worker descriptor is invalid")
    descriptor = int(raw)
    if descriptor < 3:
        raise ValueError("Bad Case worker descriptor is invalid")
    os.fstat(descriptor)
    return descriptor


def _start_parent_watchdog(descriptor: int) -> None:
    process_group_id = os.getpgrp()
    if process_group_id != os.getpid():
        raise RuntimeError("Bad Case worker lacks an isolated process group")

    def watch() -> None:
        try:
            while os.read(descriptor, 1):
                pass
        except OSError:
            pass
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        # Parent disappearance is a cancellation boundary. SIGKILL avoids a
        # stuck Python/native operation keeping the run lock indefinitely.
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    threading.Thread(
        target=watch,
        name="bad-case-parent-watchdog",
        daemon=True,
    ).start()


if __name__ == "__main__":
    main()
