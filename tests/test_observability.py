from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from search_quality.catalog import cli as catalog_cli
from search_quality.data import cli as data_cli
from search_quality.evaluation import cli as evaluation_cli
from search_quality.evaluation import compare_cli
from search_quality.observability import (
    REDACTED,
    classify_error,
    configure_logging,
    logging_context,
    normalize_trace_id,
    parse_module_levels,
)
from search_quality.smoke import run_smoke


def test_json_log_contains_context_and_redacts_sensitive_fields() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"evaluation": "INFO"},
        log_format="json",
        stream=stream,
    )
    with logging_context(trace_id="trace-123", profile_id="smoke"):
        logging.getLogger("search_quality.evaluation").info(
            "baseline_complete",
            extra={
                "run_id": "random-example",
                "query_id": 17,
                "query_text": "private search text",
                "Authorization": "Bearer secret-value",
                "error": "Bearer exception-secret",
                "nested": {
                    "openai_api_key": "openai-secret",
                    "privateKey": "private-key-secret",
                    "raw-body": "raw-body-secret",
                    "service_credentials": "credential-secret",
                    "x-api-key": "header-secret",
                    "count": 3,
                },
                "query_token_count": 2,
                "query_terms": ["private", "tokens"],
                "token_value": "token-value-secret",
            },
        )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "baseline_complete"
    assert payload["module"] == "evaluation"
    assert payload["trace_id"] == "trace-123"
    assert payload["profile_id"] == "smoke"
    assert payload["query_id"] == 17
    assert payload["query_text"] == REDACTED
    assert payload["Authorization"] == REDACTED
    assert payload["error"] == REDACTED
    assert payload["nested"] == {
        "count": 3,
        "openai_api_key": REDACTED,
        "privateKey": REDACTED,
        "raw-body": REDACTED,
        "service_credentials": REDACTED,
        "x-api-key": REDACTED,
    }
    assert payload["query_token_count"] == 2
    assert payload["query_terms"] == REDACTED
    assert payload["token_value"] == REDACTED
    assert "private search text" not in stream.getvalue()
    assert "secret-value" not in stream.getvalue()
    assert "exception-secret" not in stream.getvalue()
    assert "openai-secret" not in stream.getvalue()
    assert "private-key-secret" not in stream.getvalue()
    assert "raw-body-secret" not in stream.getvalue()
    assert "credential-secret" not in stream.getvalue()
    assert "header-secret" not in stream.getvalue()
    assert "token-value-secret" not in stream.getvalue()
    assert payload["timestamp_utc"].endswith("Z")


def test_free_form_event_is_redacted_instead_of_logging_exception_text() -> None:
    stream = io.StringIO()
    configure_logging(default_level="ERROR", stream=stream)
    logging.getLogger("search_quality.data").error(
        "Authorization: Bearer free-form-secret"
    )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == REDACTED
    assert payload["error_code"] == "unsafe_log_event_redacted"
    assert "free-form-secret" not in stream.getvalue()


def test_reserved_fields_cannot_override_event_or_context_trace() -> None:
    stream = io.StringIO()
    configure_logging(default_level="ERROR", stream=stream)
    with logging_context(
        trace_id="trusted-trace",
        event="context-private-secret",
        timestamp_utc="context-spoofed-time",
    ):
        logging.getLogger("search_quality.api").error(
            "safe_event",
            extra={
                "event": "extra-private-secret",
                "timestamp_utc": "extra-spoofed-time",
                "trace_id": "spoofed-trace",
            },
        )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "safe_event"
    assert payload["trace_id"] == "trusted-trace"
    assert payload["timestamp_utc"].endswith("Z")
    assert "private-secret" not in stream.getvalue()
    assert "spoofed" not in stream.getvalue()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (FileNotFoundError("private path"), "input_file_missing"),
        (PermissionError("private path"), "permission_denied"),
        (ValueError("private input"), "invalid_input"),
        (TypeError("private input"), "invalid_input"),
        (RuntimeError("private state"), "runtime_guard_failed"),
        (OSError("private device"), "io_failure"),
        (Exception("private cause"), "internal_error"),
    ],
)
def test_exception_classification_never_depends_on_message(
    exc: BaseException, expected: str
) -> None:
    assert classify_error(exc) == expected


def test_modules_can_be_enabled_and_disabled_independently() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="ERROR",
        module_levels={"evaluation": "DEBUG", "ranking": "OFF"},
        stream=stream,
    )
    logging.getLogger("search_quality.evaluation").debug("evaluation_debug")
    logging.getLogger("search_quality.ranking").error("ranking_error")
    logging.getLogger("search_quality.data").warning("data_warning")

    contents = stream.getvalue()
    assert "evaluation_debug" in contents
    assert "ranking_error" not in contents
    assert "data_warning" not in contents


def test_reconfiguration_does_not_duplicate_handlers() -> None:
    stream = io.StringIO()
    configure_logging(default_level="INFO", stream=stream)
    configure_logging(default_level="INFO", stream=stream)
    logging.getLogger("search_quality.api").info("request_complete")
    assert stream.getvalue().count("request_complete") == 1


def test_module_override_parser_rejects_unknown_or_invalid_values() -> None:
    assert parse_module_levels(
        ["evaluation=DEBUG", "ranking=OFF", "agent_tools=INFO"]
    ) == {
        "agent_tools": "INFO",
        "evaluation": "DEBUG",
        "ranking": "OFF",
    }
    with pytest.raises(ValueError, match="MODULE=LEVEL"):
        parse_module_levels(["unknown=INFO"])
    with pytest.raises(ValueError, match="invalid log level"):
        parse_module_levels(["api=LOUD"])


def test_trace_id_accepts_safe_values_and_replaces_unsafe_values() -> None:
    assert normalize_trace_id("request-123:child") == "request-123:child"
    generated = normalize_trace_id("bad value with spaces")
    assert len(generated) == 32
    assert generated != "bad value with spaces"


@pytest.mark.parametrize(
    "cli_module", [catalog_cli, data_cli, evaluation_cli, compare_cli]
)
def test_cli_failure_boundary_never_logs_exception_message(
    cli_module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_args) -> None:
        raise RuntimeError("Authorization: Bearer cli-private-secret")

    monkeypatch.setattr(cli_module, "_execute", fail)
    monkeypatch.setattr(sys, "argv", ["diagnostic-test"])
    with pytest.raises(SystemExit) as captured:
        cli_module.main()

    stderr = capsys.readouterr().err
    assert captured.value.code == 1
    assert "cli-private-secret" not in stderr
    event = json.loads(stderr)
    assert event["error_code"] == "runtime_guard_failed"
    assert event["error_type"] == "RuntimeError"


def test_backend_failure_boundary_never_logs_sensitive_path(tmp_path) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"backend": "ERROR"},
        stream=stream,
    )
    missing = tmp_path / "Bearer-backend-private-secret.json"

    with pytest.raises(FileNotFoundError):
        run_smoke(sample_path=missing)

    assert "backend-private-secret" not in stream.getvalue()
    event = json.loads(stream.getvalue())
    assert event["event"] == "smoke_search_failed"
    assert event["error_code"] == "input_file_missing"
    assert event["error_type"] == "FileNotFoundError"
