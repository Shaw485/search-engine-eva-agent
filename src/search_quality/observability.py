"""Low-noise structured diagnostics for project entry points.

Library modules emit named events but never configure global/root logging. CLI
and API entry points configure only the ``search_quality`` logger tree, keeping
normal machine output on stdout and diagnostics on stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import logging
import math
import os
import re
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, TextIO

LOG_MODULES = frozenset(
    {
        "agent_model",
        "agent_provider",
        "agent_eval",
        "agent_optimization",
        "agent_replay",
        "agent_runtime",
        "agent_tools",
        "agent_trace",
        "api",
        "backend",
        "bad_case",
        "bad_case_supervisor",
        "bad_case_worker",
        "catalog",
        "catalog_index",
        "catalog_pipeline",
        "catalog_serving",
        "data",
        "diagnostic_experiments",
        "evaluation",
        "human_oracle",
        "ranking",
        "query_constructor",
        "retrieval",
        "retrieval_analysis",
        "retrieval_release",
        "stage_diagnosis",
    }
)
OFF_LEVEL = logging.CRITICAL + 10
REDACTED = "[REDACTED]"
_MAX_TEXT_LENGTH = 1_000
_TRACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_SAFE_EVENT_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "search_quality_log_context", default=None
)
_STANDARD_RECORD_KEYS = frozenset(
    vars(
        logging.LogRecord(
            name="",
            level=0,
            pathname="",
            lineno=0,
            msg="",
            args=(),
            exc_info=None,
        )
    )
) | {"message", "asctime"}
_RESERVED_PAYLOAD_KEYS = frozenset(
    {"event", "level", "logger", "module", "timestamp_utc", "trace_id"}
)
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "cookie",
        "credential",
        "credentials",
        "description",
        "error",
        "error_message",
        "exception_message",
        "password",
        "payload",
        "private_key",
        "raw_body",
        "product_title",
        "query",
        "query_text",
        "request_body",
        "response_body",
        "secret",
        "secret_key",
        "title",
        "token",
        "username",
        "vector",
        "x_api_key",
    }
)
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_cookie",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_secret_key",
    "_token",
)
_SENSITIVE_COMPACT_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "clientsecret",
        "cookie",
        "password",
        "privatekey",
        "secret",
        "secretkey",
        "token",
        "xapikey",
    }
)
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "pwd",
        "secret",
    }
)
_SAFE_TEXT_METADATA_SUFFIXES = frozenset(
    {"count", "dimensions", "hash", "id", "length", "sha256", "type"}
)


def _is_sensitive_key(key: str) -> bool:
    separated_camel_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    normalized = re.sub(r"[^a-z0-9]+", "_", separated_camel_case.lower()).strip("_")
    compact = normalized.replace("_", "")
    parts = normalized.split("_") if normalized else []
    pairs = set(zip(parts, parts[1:], strict=False))
    return (
        normalized in _SENSITIVE_EXACT_KEYS
        or compact in _SENSITIVE_COMPACT_KEYS
        or any(normalized.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)
        or bool(set(parts) & _SENSITIVE_KEY_PARTS)
        or bool(pairs & {("api", "key"), ("private", "key"), ("secret", "key")})
        or (
            bool(set(parts) & {"query", "title", "token"})
            and parts[-1] not in _SAFE_TEXT_METADATA_SUFFIXES
        )
    )


def _safe_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[NON_FINITE]"
    if isinstance(value, str):
        if len(value) <= _MAX_TEXT_LENGTH:
            return value
        return value[:_MAX_TEXT_LENGTH] + "…"
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_value(str(item_key), item)
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_value(key, item) for item in value]
    return _safe_value(key, str(value))


def _record_payload(record: logging.LogRecord) -> dict[str, Any]:
    timestamp = (
        datetime.fromtimestamp(record.created, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    module = record.name.removeprefix("search_quality.").split(".", maxsplit=1)[0]
    message = record.getMessage()
    safe_event = message if _SAFE_EVENT_PATTERN.fullmatch(message) else REDACTED
    payload: dict[str, Any] = {}
    for key, value in vars(record).items():
        if key not in _STANDARD_RECORD_KEYS and key not in _RESERVED_PAYLOAD_KEYS:
            payload[key] = _safe_value(key, value)
    context = _LOG_CONTEXT.get() or {}
    for key, value in context.items():
        if key not in _RESERVED_PAYLOAD_KEYS:
            payload[key] = _safe_value(key, value)
    payload.update(
        {
            "timestamp_utc": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "module": module,
            "event": safe_event,
        }
    )
    if "trace_id" in context:
        payload["trace_id"] = _safe_value("trace_id", context["trace_id"])
    if safe_event == REDACTED:
        payload["error_code"] = "unsafe_log_event_redacted"
    if record.exc_info and record.exc_info[0] is not None:
        payload.setdefault("error_type", record.exc_info[0].__name__)
    return payload


class JsonLogFormatter(logging.Formatter):
    """One JSON object per event with recursive sensitive-field redaction."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            _record_payload(record),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class TextLogFormatter(logging.Formatter):
    """Readable local format containing the same safe structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = _record_payload(record)
        prefix = " ".join(
            str(payload.pop(key))
            for key in ("timestamp_utc", "level", "logger", "event")
        )
        details = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
            for key, value in sorted(payload.items())
        )
        return f"{prefix} {details}".rstrip()


def parse_log_level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    normalized = value.strip().upper()
    if normalized == "OFF":
        return OFF_LEVEL
    level = logging.getLevelNamesMapping().get(normalized)
    if not isinstance(level, int):
        choices = "DEBUG, INFO, WARNING, ERROR, CRITICAL, OFF"
        raise ValueError(f"invalid log level {value!r}; expected one of {choices}")
    return level


def parse_module_levels(items: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in items:
        module, separator, level = item.partition("=")
        module = module.strip().lower()
        if separator != "=" or module not in LOG_MODULES or not level.strip():
            allowed = ", ".join(sorted(LOG_MODULES))
            raise ValueError(
                f"invalid module log override {item!r}; use MODULE=LEVEL for {allowed}"
            )
        parse_log_level(level)
        overrides[module] = level.strip().upper()
    return overrides


def classify_error(exc: BaseException) -> str:
    """Map exceptions to stable, non-sensitive diagnostic codes."""

    if isinstance(exc, FileNotFoundError):
        return "input_file_missing"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_input"
    if isinstance(exc, RuntimeError):
        return "runtime_guard_failed"
    if isinstance(exc, OSError):
        return "io_failure"
    return "internal_error"


def configure_logging(
    *,
    default_level: str | int | None = None,
    module_levels: Mapping[str, str | int] | None = None,
    log_format: str | None = None,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure only project loggers and return the namespace logger."""

    configured_default = (
        default_level
        if default_level is not None
        else os.environ.get("SEARCH_LOG_LEVEL", "WARNING")
    )
    configured_format = (
        log_format or os.environ.get("SEARCH_LOG_FORMAT", "json")
    ).lower()
    if configured_format not in {"json", "text"}:
        raise ValueError("SEARCH_LOG_FORMAT must be 'json' or 'text'")

    namespace = logging.getLogger("search_quality")
    namespace.handlers.clear()
    namespace.setLevel(parse_log_level(configured_default))
    namespace.propagate = False
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        JsonLogFormatter() if configured_format == "json" else TextLogFormatter()
    )
    namespace.addHandler(handler)

    explicit = {key.lower(): value for key, value in (module_levels or {}).items()}
    unknown = sorted(set(explicit) - LOG_MODULES)
    if unknown:
        raise ValueError(f"unknown log modules: {unknown}")
    for module in LOG_MODULES:
        module_logger = logging.getLogger(f"search_quality.{module}")
        module_logger.disabled = False
        module_logger.propagate = True
        environment_level = os.environ.get(f"SEARCH_LOG_LEVEL_{module.upper()}")
        configured_level = explicit.get(module, environment_level)
        module_logger.setLevel(
            logging.NOTSET
            if configured_level is None
            else parse_log_level(configured_level)
        )
    return namespace


def add_logging_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        help="default diagnostics level: DEBUG/INFO/WARNING/ERROR/CRITICAL/OFF",
    )
    parser.add_argument(
        "--log-module",
        action="append",
        default=[],
        metavar="MODULE=LEVEL",
        help="repeat to override one diagnostics module independently",
    )
    parser.add_argument(
        "--log-format",
        choices=("json", "text"),
        help="diagnostics format; defaults to SEARCH_LOG_FORMAT or json",
    )


def configure_logging_from_args(args: argparse.Namespace) -> logging.Logger:
    return configure_logging(
        default_level=getattr(args, "log_level", None),
        module_levels=parse_module_levels(getattr(args, "log_module", [])),
        log_format=getattr(args, "log_format", None),
    )


def new_trace_id() -> str:
    return uuid.uuid4().hex


def normalize_trace_id(candidate: str | None) -> str:
    if candidate and _TRACE_ID_PATTERN.fullmatch(candidate):
        return candidate
    return new_trace_id()


def current_trace_id() -> str:
    value = (_LOG_CONTEXT.get() or {}).get("trace_id")
    return str(value) if value else "unavailable"


@contextlib.contextmanager
def logging_context(**fields: Any) -> Iterator[None]:
    current = _LOG_CONTEXT.get() or {}
    token = _LOG_CONTEXT.set({**current, **fields})
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)
