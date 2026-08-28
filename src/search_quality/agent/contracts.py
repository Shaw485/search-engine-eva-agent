"""Strict, provider-independent contracts for the Agent runtime."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

RUN_ID_PATTERN = r"[a-z][a-z0-9-]{0,31}-[0-9a-f]{12}"
SAFE_ID_PATTERN = r"[a-z][a-z0-9_-]{0,63}"
REASON_CODE_PATTERN = r"[a-z][a-z0-9_]{0,63}"
RUN_ID_FIELD_PATTERN = rf"^(?:{RUN_ID_PATTERN})$"
SAFE_ID_FIELD_PATTERN = rf"^(?:{SAFE_ID_PATTERN})$"
REASON_CODE_FIELD_PATTERN = rf"^(?:{REASON_CODE_PATTERN})$"
EVIDENCE_REF_PATTERN = re.compile(
    r"(?:run|comparison|query):[A-Za-z0-9][A-Za-z0-9:._-]{0,191}\Z"
)


class StrictModel(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentState(StrEnum):
    PLANNING = "planning"
    ACTING = "acting"
    OBSERVING = "observing"
    DECIDING = "deciding"
    COMPLETED = "completed"
    FAILED = "failed"


class TerminalOutcome(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    INCONCLUSIVE = "inconclusive"
    PROPOSAL_READY = "proposal_ready"
    NO_SAFE_IMPROVEMENT = "no_safe_improvement"


class AgentTask(StrictModel):
    """One narrow comparison task accepted by the Stage 3 runtime."""

    task_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    task_type: Literal["compare_runs"] = "compare_runs"
    baseline_run_id: StrictStr = Field(pattern=RUN_ID_FIELD_PATTERN)
    candidate_run_id: StrictStr = Field(pattern=RUN_ID_FIELD_PATTERN)
    primary_metric: Literal["ndcg@10"] = "ndcg@10"
    max_regressions_to_inspect: StrictInt = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def validate_run_pair(self) -> Self:
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("comparison Runs must differ")
        return self


RetrievalPipelineVariant = Literal[
    "title-exact-multifield-v1",
    "title-exact-multifield-weighted-v1",
    "title-exact-multifield-weighted-aggressive-v1",
]

RETRIEVAL_PIPELINE_VARIANTS: tuple[RetrievalPipelineVariant, ...] = (
    "title-exact-multifield-v1",
    "title-exact-multifield-weighted-v1",
    "title-exact-multifield-weighted-aggressive-v1",
)


class RetrievalOptimizationTask(StrictModel):
    """One smoke-only stage diagnosis and bounded retrieval optimization task."""

    task_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    task_type: Literal["optimize_retrieval_stages"] = "optimize_retrieval_stages"
    profile: Literal["smoke"] = "smoke"
    objective: Literal["expand_recall_without_downstream_regression"] = (
        "expand_recall_without_downstream_regression"
    )
    candidate_variants: tuple[RetrievalPipelineVariant, ...] = (
        RETRIEVAL_PIPELINE_VARIANTS
    )

    @model_validator(mode="after")
    def validate_candidate_space(self) -> Self:
        if self.candidate_variants != RETRIEVAL_PIPELINE_VARIANTS:
            raise ValueError("retrieval candidate space must use the trusted order")
        return self


RuntimeTask = AgentTask | RetrievalOptimizationTask


class ToolAction(StrictModel):
    kind: Literal["tool"] = "tool"
    tool_name: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    arguments: dict[str, Any]
    reason_code: StrictStr = Field(pattern=REASON_CODE_FIELD_PATTERN)


class FinishDecision(StrictModel):
    kind: Literal["finish"] = "finish"
    outcome: TerminalOutcome
    evidence_refs: list[StrictStr] = Field(max_length=16)
    reason_code: StrictStr = Field(pattern=REASON_CODE_FIELD_PATTERN)


AgentDecision = ToolAction | FinishDecision


class ToolObservation(StrictModel):
    tool_name: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    status: Literal["succeeded", "failed"]
    evidence_ref: StrictStr | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    error_code: StrictStr | None = Field(
        default=None, pattern=REASON_CODE_FIELD_PATTERN
    )
    retryable: bool = False
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    def canonical_payload(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", exclude={"sha256"}),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")


class TerminalResult(StrictModel):
    trace_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    state: Literal["completed", "failed"]
    outcome: TerminalOutcome
    evidence_refs: list[StrictStr]
    reason_code: StrictStr = Field(pattern=REASON_CODE_FIELD_PATTERN)
    report: dict[str, Any]
    steps_used: StrictInt = Field(ge=0)
    tool_calls_used: StrictInt = Field(ge=0)


def validate_evidence_ref(value: str) -> str:
    if not EVIDENCE_REF_PATTERN.fullmatch(value):
        raise ValueError("invalid evidence reference")
    return value


def ensure_json_value(value: Any) -> None:
    """Reject non-JSON and non-finite observation/action content."""

    json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
