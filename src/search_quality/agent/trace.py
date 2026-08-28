"""Hash-chained Agent Trace artifacts and confined local storage."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, StrictFloat, StrictInt, StrictStr

from search_quality.evaluation.artifacts import write_immutable_text

from .contracts import (
    AgentDecision,
    AgentState,
    RuntimeTask,
    StrictModel,
    TerminalResult,
    ToolObservation,
    ensure_json_value,
)

TRACE_SCHEMA_VERSION = "search-evaluation-agent-trace-v1"
TRACE_ID_PATTERN = r"[0-9a-f]{32}"
# The default runtime can record six observations of up to 1 MiB, while the
# terminal report can repeat the final comparison payload. Keep the artifact
# ceiling above that valid worst case, but enforce the same ceiling on write
# and read.
MAX_TRACE_BYTES = 8 * 1024 * 1024
ZERO_HASH = "0" * 64
logger = logging.getLogger("search_quality.agent_trace")


class TraceEvent(StrictModel):
    sequence: StrictInt = Field(ge=1)
    timestamp_utc: StrictStr
    state_before: AgentState
    event_type: Literal[
        "action_selected",
        "tool_observed",
        "observation_processed",
        "run_completed",
        "run_failed",
    ]
    state_after: AgentState
    decision: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    duration_ms: StrictFloat = Field(ge=0.0)
    context_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminal_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    previous_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class AgentTrace(StrictModel):
    schema_version: Literal[TRACE_SCHEMA_VERSION] = TRACE_SCHEMA_VERSION
    trace_id: StrictStr = Field(pattern=rf"^{TRACE_ID_PATTERN}$")
    runtime_id: Literal["search-agent-runtime-v1"] = "search-agent-runtime-v1"
    planner_id: StrictStr
    task: RuntimeTask
    policy: dict[str, Any]
    tool_names: list[StrictStr]
    events: list[TraceEvent] = Field(min_length=1)
    terminal: TerminalResult


def canonical_json_bytes(value: Any) -> bytes:
    ensure_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def compute_event_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def compute_terminal_hash(terminal: TerminalResult) -> str:
    """Return the canonical digest bound into the final Trace event."""

    return hashlib.sha256(
        canonical_json_bytes(terminal.model_dump(mode="json"))
    ).hexdigest()


def compute_trace_context_hash(
    *,
    trace_id: str,
    planner_id: str,
    task: RuntimeTask,
    policy: dict[str, Any],
    tool_names: list[str],
) -> str:
    """Bind immutable execution context to the final event checksum."""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "planner_id": planner_id,
                "policy": policy,
                "runtime_id": "search-agent-runtime-v1",
                "schema_version": TRACE_SCHEMA_VERSION,
                "task": task.model_dump(mode="json"),
                "tool_names": tool_names,
                "trace_id": trace_id,
            }
        )
    ).hexdigest()


class TraceRecorder:
    def __init__(self, trace_id: str, *, context_sha256: str) -> None:
        self.trace_id = trace_id
        if not re_fullmatch_sha256(context_sha256):
            raise ValueError("invalid Trace context digest")
        self.context_sha256 = context_sha256
        self._events: list[TraceEvent] = []

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def append(
        self,
        *,
        state_before: AgentState,
        event_type: str,
        state_after: AgentState,
        decision: AgentDecision | None = None,
        observation: ToolObservation | None = None,
        terminal: TerminalResult | None = None,
        duration_ms: float = 0.0,
    ) -> TraceEvent:
        is_terminal_event = event_type in {"run_completed", "run_failed"}
        if is_terminal_event != (terminal is not None):
            raise ValueError("Terminal result is required only for a final event")
        previous_hash = self._events[-1].event_hash if self._events else ZERO_HASH
        payload: dict[str, Any] = {
            "context_sha256": self.context_sha256 if is_terminal_event else None,
            "decision": (
                decision.model_dump(mode="json") if decision is not None else None
            ),
            "duration_ms": float(duration_ms),
            "event_type": event_type,
            "observation": (
                observation.model_dump(mode="json") if observation is not None else None
            ),
            "previous_hash": previous_hash,
            "sequence": len(self._events) + 1,
            "state_after": state_after.value,
            "state_before": state_before.value,
            "terminal_sha256": (
                compute_terminal_hash(terminal) if terminal is not None else None
            ),
            "timestamp_utc": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
        event_hash = compute_event_hash(payload)
        event = TraceEvent.model_validate({**payload, "event_hash": event_hash})
        self._events.append(event)
        return event


class TraceStore:
    def __init__(self, root: str | Path) -> None:
        configured = Path(root)
        if configured.is_symlink():
            raise ValueError("Trace store must not be a symbolic link")
        configured.mkdir(parents=True, exist_ok=True)
        self.root = configured.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Trace store must be a directory")

    def store(self, trace: AgentTrace) -> Path:
        self._require_trusted_root()
        path = self.root / f"trace-{trace.trace_id}.json"
        if path.is_symlink():
            raise ValueError("Trace artifact must not be a symbolic link")
        serialized = (
            json.dumps(
                trace.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
        if len(serialized.encode("utf-8")) > MAX_TRACE_BYTES:
            raise ValueError("Trace artifact exceeds the size limit")
        write_immutable_text(path, serialized)
        logger.info(
            "agent_trace_stored",
            extra={
                "event_count": len(trace.events),
                "terminal_state": trace.terminal.state,
            },
        )
        return path

    def load(self, trace_id: str) -> AgentTrace:
        if not isinstance(trace_id, str) or not re_fullmatch_trace_id(trace_id):
            raise ValueError("invalid Trace ID")
        self._require_trusted_root()
        path = self.root / f"trace-{trace_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Trace artifact is unavailable")
        with path.open("rb") as handle:
            encoded = handle.read(MAX_TRACE_BYTES + 1)
        if len(encoded) > MAX_TRACE_BYTES:
            raise ValueError("Trace artifact exceeds the size limit")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Trace JSON contains duplicate keys")
                result[key] = value
            return result

        def reject_constant(_value: str) -> None:
            raise ValueError("Trace JSON contains non-finite numbers")

        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
        trace = AgentTrace.model_validate(payload)
        if trace.trace_id != trace_id:
            raise ValueError("Trace filename does not match its ID")
        return trace

    def _require_trusted_root(self) -> None:
        try:
            if (
                self.root.is_symlink()
                or not self.root.is_dir()
                or self.root.resolve(strict=True) != self.root
            ):
                raise ValueError("Trace store root changed after initialization")
        except OSError as exc:
            raise ValueError("Trace store root is unavailable") from exc


def re_fullmatch_trace_id(value: str) -> bool:
    return len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


def re_fullmatch_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
