from __future__ import annotations

import hashlib
import io
import json
import struct
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from search_quality.bad_cases import supervisor as bad_supervisor
from search_quality.bad_cases.artifacts import bad_case_run_lock
from search_quality.bad_cases.runner import run_bad_case_diagnostics
from search_quality.bad_cases.supervisor import (
    MAX_SUPERVISOR_RECEIPT_BYTES,
    MAX_WORKER_ENVELOPE_BYTES,
    BadCaseWorkerDeadlineExceeded,
    BadCaseWorkerProtocolError,
    WorkerProcessResult,
    _build_worker_environment,
    _decode_worker_envelope,
    _execute_worker_process,
    _recover_completed_run,
    _store_supervisor_attempt_if_no_terminal,
    _store_supervisor_execution_receipt,
    bad_case_supervisor_lock,
    load_supervisor_execution_receipt,
)
from search_quality.bad_cases.worker_contracts import (
    BadCaseSupervisorExecutionReceipt,
    BadCaseWorkerAttempt,
    BadCaseWorkerFailed,
    BadCaseWorkerRequest,
)
from search_quality.catalog.index import build_catalog_index
from search_quality.catalog.search import CatalogSearchService
from search_quality.data.contracts import canonical_json_sha256
from search_quality.observability import configure_logging

ROOT = Path(__file__).resolve().parents[1]
WORKER_FIXTURE = ROOT / "tests" / "fixtures" / "bad_case_worker_process.py"
REVISION = "a" * 40


def _run_fixture(
    mode: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
    deadline_ms: int = 1_000,
    term_grace_ms: int = 200,
    kill_grace_ms: int = 800,
):
    return _execute_worker_process(
        command=(sys.executable, "-I", str(WORKER_FIXTURE), mode, *arguments),
        environment=environment or {},
        deadline_ms=deadline_ms,
        term_grace_ms=term_grace_ms,
        kill_grace_ms=kill_grace_ms,
    )


def _generic_payload(framed: bytes) -> dict[str, object]:
    assert len(framed) >= 4
    expected = struct.unpack(">I", framed[:4])[0]
    assert expected == len(framed[4:])
    payload = json.loads(framed[4:])
    assert isinstance(payload, dict)
    return payload


def _request(tmp_path: Path) -> BadCaseWorkerRequest:
    return BadCaseWorkerRequest(
        execution_id="bad-case-execution-" + ("b" * 32),
        trace_id="worker-test-trace",
        execution_started_at_utc=datetime.now(UTC),
        project_root=str(ROOT),
        artifact_root=str(tmp_path / "runs"),
        catalog_index_path=str(tmp_path / "catalog.sqlite3"),
        executor_revision=REVISION,
        deadline_ms=1_000,
    )


def _small_catalog_index(tmp_path: Path) -> Path:
    source = tmp_path / "products.parquet"
    pl.DataFrame(
        {
            "product_id": ["B000WORKER1"],
            "product_locale": ["us"],
            "product_title": ["Wireless Mouse"],
            "product_description": [None],
            "product_bullet_point": [None],
            "product_brand": ["Worker"],
            "product_color": ["Black"],
        }
    ).write_parquet(source)
    encoded = source.read_bytes()
    output = tmp_path / "catalog.sqlite3"
    build_catalog_index(
        source,
        output,
        expected_source_size=len(encoded),
        expected_source_sha256=hashlib.sha256(encoded).hexdigest(),
        expected_product_count=1,
        code_revision=REVISION,
        batch_size=1,
    )
    return output


def _completed_child_run(
    tmp_path: Path,
    *,
    execution_hex: str = "d" * 32,
):
    index = _small_catalog_index(tmp_path)
    run = run_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "runs",
        revision_provider=lambda _root: REVISION,
        search_service=CatalogSearchService(index),
        execution_id=f"bad-case-execution-{execution_hex}",
        execution_started_at_utc=datetime.now(UTC),
    )
    return run, index


def _receipt_request(tmp_path: Path, *, execution_id: str) -> BadCaseWorkerRequest:
    return _request(tmp_path).model_copy(
        update={
            "execution_id": execution_id,
            "artifact_root": str(tmp_path / "runs"),
            "trace_id": "receipt-test-trace",
        }
    )


def test_real_isolated_worker_completes_and_preserves_parent_execution_id(
    tmp_path: Path,
) -> None:
    from search_quality.bad_cases.supervisor import supervise_bad_case_diagnostics

    run = supervise_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "runs",
        catalog_index_path=_small_catalog_index(tmp_path),
        executor_revision=REVISION,
        deadline_ms=10_000,
        term_grace_ms=200,
        kill_grace_ms=800,
        trace_id="real-worker-test",
    )

    assert run.artifact.completed is True
    assert run.artifact.query_count == 59
    assert run.execution.execution_id.startswith("bad-case-execution-")
    assert Path(run.execution_path).is_file()
    assert Path(run.artifact_path).is_file()

    supervisor_receipt = load_supervisor_execution_receipt(
        tmp_path / "runs",
        run.execution.execution_id,
    )
    assert supervisor_receipt.completed is True
    assert supervisor_receipt.execution_id == run.execution.execution_id
    assert supervisor_receipt.diagnostic_id == run.artifact.diagnostic_id
    assert supervisor_receipt.child_execution_id == run.execution.execution_id
    assert supervisor_receipt.policy_id == "posix-process-group-deadline-v1"
    assert supervisor_receipt.deadline_ms == 10_000
    assert supervisor_receipt.term_grace_ms == 200
    assert supervisor_receipt.kill_grace_ms == 800
    assert supervisor_receipt.completion_observation == "worker_result"
    assert supervisor_receipt.trace_id == "real-worker-test"
    expected_receipt_id = (
        "bad-case-supervisor-execution-"
        + canonical_json_sha256(
            supervisor_receipt.model_dump(mode="json", exclude={"receipt_id"})
        )[:12]
    )
    assert supervisor_receipt.receipt_id == expected_receipt_id
    supervisor_path = (
        tmp_path
        / "runs"
        / "bad-case-diagnostics"
        / "supervisor-executions"
        / f"{run.execution.execution_id}.json"
    )
    assert supervisor_path.stat().st_mode & 0o077 == 0
    assert supervisor_path.parent.stat().st_mode & 0o077 == 0

    recovered = _recover_completed_run(
        run_root=tmp_path / "runs",
        execution_id=run.execution.execution_id,
    )
    assert recovered is not None
    assert recovered.artifact == run.artifact
    assert recovered.execution == run.execution
    assert recovered.samples == []

    request = _request(tmp_path).model_copy(
        update={
            "execution_id": run.execution.execution_id,
            "execution_started_at_utc": run.execution.started_at_utc,
            "artifact_root": str(tmp_path / "runs"),
        }
    )
    stored = _store_supervisor_attempt_if_no_terminal(
        run_root=tmp_path / "runs",
        request=request,
        status="timed_out",
        failure_stage="worker_deadline",
        error_code="worker_deadline_exceeded",
        completed_query_count=None,
        count_semantics="unknown",
        termination_signal="SIGKILL",
        kill_escalated=True,
        worker_exit_code=-9,
        duration_ms=126_000.0,
    )
    assert stored is None
    attempt = (
        tmp_path
        / "runs"
        / "bad-case-diagnostics"
        / "attempts"
        / f"{run.execution.execution_id}.json"
    )
    assert not attempt.exists()

    identical = _store_supervisor_execution_receipt(
        run_root=tmp_path / "runs",
        request=request.model_copy(
            update={
                "trace_id": "real-worker-test",
                "deadline_ms": 10_000,
            }
        ),
        durable_run=recovered,
        term_grace_ms=200,
        kill_grace_ms=800,
        completion_observation="worker_result",
    )
    assert identical == supervisor_receipt
    with pytest.raises(BadCaseWorkerProtocolError, match="conflicting supervisor"):
        _store_supervisor_execution_receipt(
            run_root=tmp_path / "runs",
            request=request.model_copy(
                update={
                    "trace_id": "real-worker-test",
                    "deadline_ms": 9_999,
                }
            ),
            durable_run=recovered,
            term_grace_ms=200,
            kill_grace_ms=800,
            completion_observation="worker_result",
        )


def test_deadline_boundary_recovery_publishes_supervisor_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_hex = "7" * 32
    child_run, index = _completed_child_run(
        tmp_path,
        execution_hex=execution_hex,
    )
    monkeypatch.setattr(bad_supervisor, "new_trace_id", lambda: execution_hex)
    monkeypatch.setattr(
        bad_supervisor,
        "_execute_worker_process",
        lambda **_kwargs: WorkerProcessResult(
            returncode=-15,
            framed_payload=b"",
            payload_overflow=False,
            timed_out=True,
            termination_signal="SIGTERM",
            kill_escalated=False,
            process_group_alive=False,
            duration_ms=1_001.0,
        ),
    )

    recovered = bad_supervisor.supervise_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "runs",
        catalog_index_path=index,
        executor_revision=REVISION,
        deadline_ms=1_000,
        term_grace_ms=111,
        kill_grace_ms=222,
        trace_id="deadline-boundary-trace",
    )

    assert recovered.artifact == child_run.artifact
    assert recovered.execution == child_run.execution
    assert recovered.samples == []
    receipt = load_supervisor_execution_receipt(
        tmp_path / "runs",
        child_run.execution.execution_id,
    )
    assert receipt.completion_observation == "deadline_boundary_recovery"
    assert receipt.deadline_ms == 1_000
    assert receipt.term_grace_ms == 111
    assert receipt.kill_grace_ms == 222
    assert receipt.trace_id == "deadline-boundary-trace"


def test_supervisor_receipt_contract_rejects_non_content_addressed_id(
    tmp_path: Path,
) -> None:
    child_run, _index = _completed_child_run(tmp_path)
    request = _receipt_request(
        tmp_path,
        execution_id=child_run.execution.execution_id,
    )
    receipt = _store_supervisor_execution_receipt(
        run_root=tmp_path / "runs",
        request=request,
        durable_run=child_run,
        term_grace_ms=200,
        kill_grace_ms=800,
        completion_observation="worker_result",
    )
    payload = receipt.model_dump(mode="json")
    payload["receipt_id"] = "bad-case-supervisor-execution-000000000000"

    with pytest.raises(ValidationError, match="does not match its content"):
        BadCaseSupervisorExecutionReceipt.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "invalid_artifact",
    ["symlink", "duplicate_keys", "oversize", "public_permissions"],
)
def test_supervisor_receipt_loader_rejects_unsafe_artifacts(
    tmp_path: Path,
    invalid_artifact: str,
) -> None:
    child_run, _index = _completed_child_run(tmp_path)
    execution_id = child_run.execution.execution_id
    base = tmp_path / "runs" / "bad-case-diagnostics"
    receipt_dir = base / "supervisor-executions"
    receipt_dir.mkdir(mode=0o700)
    path = receipt_dir / f"{execution_id}.json"

    if invalid_artifact == "symlink":
        path.symlink_to(child_run.execution_path)
    elif invalid_artifact == "duplicate_keys":
        path.write_text(
            '{"schema_version":"bad-case-supervisor-execution-v1",'
            '"schema_version":"bad-case-supervisor-execution-v1"}',
            encoding="utf-8",
        )
        path.chmod(0o600)
    elif invalid_artifact == "oversize":
        path.write_bytes(b"{" + (b" " * MAX_SUPERVISOR_RECEIPT_BYTES) + b"}")
        path.chmod(0o600)
    else:
        receipt = _store_supervisor_execution_receipt(
            run_root=tmp_path / "runs",
            request=_receipt_request(tmp_path, execution_id=execution_id),
            durable_run=child_run,
            term_grace_ms=200,
            kill_grace_ms=800,
            completion_observation="worker_result",
        )
        assert receipt.completed is True
        path.chmod(0o644)

    with pytest.raises((ValueError, ValidationError)):
        load_supervisor_execution_receipt(tmp_path / "runs", execution_id)


def test_supervisor_receipt_loader_accepts_only_execution_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid format"):
        load_supervisor_execution_receipt(
            tmp_path / "runs",
            "../../private-receipt",
        )


def test_true_subprocess_success_uses_one_bounded_frame() -> None:
    result = _run_fixture("success")

    assert result.returncode == 0
    assert result.timed_out is False
    assert result.payload_overflow is False
    assert result.process_group_alive is False
    assert _generic_payload(result.framed_payload) == {"status": "ok"}


def test_worker_environment_is_allowlisted_and_drops_parent_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-model-secret")
    monkeypatch.setenv("PYTHONPATH", "/private/injection")
    environment = _build_worker_environment(_request(tmp_path))

    assert "OPENAI_API_KEY" not in environment
    assert "PYTHONPATH" not in environment
    assert set(environment) <= {
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "SEARCH_BAD_CASE_REQUEST",
        "SEARCH_LOG_FORMAT",
        "SEARCH_LOG_LEVEL",
        "SEARCH_LOG_LEVEL_BAD_CASE",
        "SEARCH_LOG_LEVEL_BAD_CASE_WORKER",
    }
    result = _run_fixture("secret_absent", environment=environment)
    assert _generic_payload(result.framed_payload) == {"secret_absent": True}


def test_worker_stdout_is_discarded_not_copied_to_logs_or_response(
    capfd: pytest.CaptureFixture[str],
) -> None:
    result = _run_fixture("stdout_private")

    assert _generic_payload(result.framed_payload) == {"status": "ok"}
    captured = capfd.readouterr()
    assert "private Query" not in captured.out
    assert "private Query" not in captured.err


def test_deadline_terminates_cooperative_worker_process_group(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "sleep.pid"
    started = time.monotonic()
    result = _run_fixture(
        "sleep",
        str(marker),
        deadline_ms=1_000,
        term_grace_ms=200,
        kill_grace_ms=500,
    )

    assert marker.exists()
    assert result.timed_out is True
    assert result.termination_signal == "SIGTERM"
    assert result.kill_escalated is False
    assert result.process_group_alive is False
    assert time.monotonic() - started < 3.0


def test_deadline_escalates_to_sigkill_when_worker_ignores_sigterm(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ignore.pid"
    result = _run_fixture(
        "ignore_term",
        str(marker),
        deadline_ms=1_000,
        term_grace_ms=100,
        kill_grace_ms=800,
    )

    assert marker.exists()
    assert result.timed_out is True
    assert result.termination_signal == "SIGKILL"
    assert result.kill_escalated is True
    assert result.process_group_alive is False


def test_deadline_kills_worker_and_grandchild_as_one_process_group(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "processes.pid"
    result = _run_fixture(
        "grandchild",
        str(marker),
        deadline_ms=1_500,
        term_grace_ms=100,
        kill_grace_ms=1_500,
    )

    assert len(marker.read_text(encoding="utf-8").split()) == 2
    assert result.timed_out is True
    assert result.termination_signal == "SIGKILL"
    assert result.kill_escalated is True
    assert result.process_group_alive is False


@pytest.mark.parametrize("mode", ["malformed", "raw_private_payload"])
def test_malformed_or_private_payload_is_rejected_without_logging_content(
    mode: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = _run_fixture(mode)

    with pytest.raises((ValueError, ValidationError)):
        _decode_worker_envelope(
            result.framed_payload,
            overflow=result.payload_overflow,
        )
    logs = caplog.text
    assert "private wireless mouse Query" not in logs
    assert "private product title" not in logs


def test_oversized_worker_payload_is_drained_and_rejected() -> None:
    result = _run_fixture("oversize")

    assert result.returncode == 0
    assert result.payload_overflow is True
    assert len(result.framed_payload) <= MAX_WORKER_ENVELOPE_BYTES + 5
    with pytest.raises(ValueError, match="bounded frame"):
        _decode_worker_envelope(
            result.framed_payload,
            overflow=result.payload_overflow,
        )


def test_killed_worker_releases_existing_cross_process_run_lock(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    marker = tmp_path / "run-lock.pid"
    result = _run_fixture(
        "run_lock",
        str(run_root),
        str(marker),
        # This fixture imports the installed package in a fresh isolated
        # interpreter before taking the lock. Keep startup outside the
        # assertion under cold filesystem/import caches; the production
        # worker's policy is 125 seconds.
        deadline_ms=5_000,
        term_grace_ms=100,
        kill_grace_ms=800,
    )

    assert marker.exists()
    assert result.timed_out is True
    assert result.kill_escalated is True
    assert result.process_group_alive is False
    with bad_case_run_lock(run_root):
        pass


def test_supervisor_flock_is_visible_to_another_process(tmp_path: Path) -> None:
    run_root = tmp_path / "runs"
    with bad_case_supervisor_lock(run_root):
        result = _run_fixture(
            "try_supervisor_lock",
            str(run_root),
            deadline_ms=5_000,
        )

    assert result.returncode == 0
    assert _generic_payload(result.framed_payload) == {"status": "busy"}


def test_timeout_attempt_requires_unknown_query_count() -> None:
    payload = {
        "execution_id": "bad-case-execution-" + ("c" * 32),
        "status": "timed_out",
        "failure_stage": "worker_deadline",
        "completed_query_count": None,
        "count_semantics": "unknown",
        "error_code": "worker_deadline_exceeded",
        "deadline_ms": 125_000,
        "termination_signal": "SIGKILL",
        "kill_escalated": True,
        "worker_exit_code": -9,
        "started_at_utc": datetime.now(UTC),
        "completed_at_utc": datetime.now(UTC),
        "duration_ms": 126_000.0,
    }
    attempt = BadCaseWorkerAttempt.model_validate(payload, strict=True)
    assert attempt.completed_query_count is None
    assert attempt.count_semantics == "unknown"

    payload["completed_query_count"] = 0
    with pytest.raises(ValueError, match="count semantics"):
        BadCaseWorkerAttempt.model_validate(payload, strict=True)


def test_worker_failure_envelope_rejects_unallowlisted_error_codes() -> None:
    with pytest.raises(ValidationError):
        BadCaseWorkerFailed.model_validate(
            {
                "status": "failed",
                "execution_id": "bad-case-execution-" + ("e" * 32),
                "error_code": "private_provider_message",
            },
            strict=True,
        )


def test_supervisor_timeout_persists_unknown_count_and_no_completed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "catalog.sqlite3"
    index.write_bytes(b"fixture only")
    monkeypatch.setattr(
        bad_supervisor,
        "_execute_worker_process",
        lambda **_kwargs: WorkerProcessResult(
            returncode=-9,
            framed_payload=b"",
            payload_overflow=False,
            timed_out=True,
            termination_signal="SIGKILL",
            kill_escalated=True,
            process_group_alive=False,
            duration_ms=126_000.0,
        ),
    )

    with pytest.raises(BadCaseWorkerDeadlineExceeded) as captured:
        bad_supervisor.supervise_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=tmp_path / "runs",
            catalog_index_path=index,
            executor_revision=REVISION,
            deadline_ms=125_000,
        )

    base = tmp_path / "runs" / "bad-case-diagnostics"
    attempt_path = next((base / "attempts").glob("*.json"))
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert attempt["execution_id"] == captured.value.execution_id
    assert attempt["completed_query_count"] is None
    assert attempt["count_semantics"] == "unknown"
    assert attempt["termination_signal"] == "SIGKILL"
    assert not (base / "executions").exists()


def test_supervisor_protocol_error_never_logs_raw_worker_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "catalog.sqlite3"
    index.write_bytes(b"fixture only")
    private_query = "private wireless mouse Query"
    private_title = "private product title"
    encoded = json.dumps({"query_text": private_query, "title": private_title}).encode(
        "utf-8"
    )
    framed = struct.pack(">I", len(encoded)) + encoded
    monkeypatch.setattr(
        bad_supervisor,
        "_execute_worker_process",
        lambda **_kwargs: WorkerProcessResult(
            returncode=0,
            framed_payload=framed,
            payload_overflow=False,
            timed_out=False,
            termination_signal=None,
            kill_escalated=False,
            process_group_alive=False,
            duration_ms=1.0,
        ),
    )
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"bad_case_supervisor": "DEBUG"},
        stream=stream,
    )
    try:
        with pytest.raises(BadCaseWorkerProtocolError):
            bad_supervisor.supervise_bad_case_diagnostics(
                project_root=ROOT,
                artifact_root=tmp_path / "runs",
                catalog_index_path=index,
                executor_revision=REVISION,
                deadline_ms=1_000,
            )
    finally:
        configure_logging()

    logs = stream.getvalue()
    assert "bad_case_worker_protocol_invalid" in logs
    assert private_query not in logs
    assert private_title not in logs
