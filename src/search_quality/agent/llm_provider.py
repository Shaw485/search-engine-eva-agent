"""Hard-bounded subprocess boundary for the retrieval LLM provider.

The API process never imports the provider SDK and never stores an API key on a
provider object.  One decision request is serialized to a small JSON document,
sent to :mod:`search_quality.agent.llm_worker`, and mapped back to one trusted
option ID.  Tool names, arguments and evidence references do not cross this
boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from .contracts import SAFE_ID_FIELD_PATTERN, StrictModel

logger = logging.getLogger("search_quality.agent_provider")

OPENAI_PROVIDER_ID = "openai"
VOLCENGINE_AGENT_PLAN_PROVIDER_ID = "volcengine_agent_plan"
# Backwards-compatible export used by existing OpenAI-only callers.
PROVIDER_ID = OPENAI_PROVIDER_ID
LLMProviderId: TypeAlias = Literal[
    "openai",
    "volcengine_agent_plan",
]
DECISION_FUNCTION_NAME = "submit_retrieval_decision"
REQUEST_SCHEMA_VERSION = "retrieval-llm-decision-request-v1"
MAX_WORKER_REQUEST_BYTES = 32 * 1024
MAX_WORKER_RESPONSE_BYTES = 8 * 1024
DEFAULT_PROVIDER_TIMEOUT_MS = 30_000
WORKER_LIFECYCLE_MARGIN_MS = 10_000
DEFAULT_WORKER_TIMEOUT_MS = DEFAULT_PROVIDER_TIMEOUT_MS + WORKER_LIFECYCLE_MARGIN_MS
DEFAULT_TERMINATE_GRACE_MS = 250

RetrievalOptionId: TypeAlias = Literal[
    "diagnose_baseline",
    "run_uniform_candidate",
    "run_conservative_candidate",
    "run_aggressive_candidate",
    "finish_best_passing_candidate",
    "finish_no_safe_improvement",
]
RETRIEVAL_OPTION_IDS: tuple[RetrievalOptionId, ...] = (
    "diagnose_baseline",
    "run_uniform_candidate",
    "run_conservative_candidate",
    "run_aggressive_candidate",
    "finish_best_passing_candidate",
    "finish_no_safe_improvement",
)

RetrievalActionOptionId: TypeAlias = Literal[
    "diagnose_baseline",
    "run_uniform_candidate",
    "run_conservative_candidate",
    "run_aggressive_candidate",
]
RetrievalPipelineVariant: TypeAlias = Literal[
    "title-exact-multifield-v1",
    "title-exact-multifield-weighted-v1",
    "title-exact-multifield-weighted-aggressive-v1",
]
RetrievalGateName: TypeAlias = Literal[
    "unique_relevant_contribution",
    "union_coverage_improvement",
    "fusion_recall_at_10_floor",
    "fusion_ndcg_at_10_floor",
    "fusion_mrr_at_10_floor",
    "coarse_recall_at_10_floor",
    "coarse_ndcg_at_10_floor",
    "coarse_mrr_at_10_floor",
    "worst_query_coarse_ndcg_delta_floor",
    "regressed_query_rate_ceiling",
    "worst_query_fusion_ndcg_delta_floor",
    "fusion_regressed_query_rate_ceiling",
]

FiniteDelta = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=-1.0, le=1.0),
]
UnitFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0),
]
PositiveDuration = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0, le=120_000.0),
]
MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def provider_model_matches(
    provider: LLMProviderId,
    requested_model: str,
    reported_model: str,
) -> bool:
    """Validate model provenance without assuming provider aliases echo exactly.

    OpenAI keeps exact model binding. Volcengine's Responses contract reports
    the actual resolved model name/version, while Agent Plan accepts a stable
    package alias such as ``doubao-seed-2.1-turbo``. A resolved Volcengine
    value may therefore append a version after normalizing dots to hyphens.
    Only that exact family prefix is accepted; another tier or family still
    fails closed.
    """

    if provider == OPENAI_PROVIDER_ID:
        return reported_model == requested_model
    if provider != VOLCENGINE_AGENT_PLAN_PROVIDER_ID:
        return False

    def normalize(value: str) -> str:
        return "-".join(
            part
            for part in value.lower().replace(".", "-").replace("_", "-").split("-")
            if part
        )

    requested = normalize(requested_model)
    reported = normalize(reported_model)
    return reported == requested or reported.startswith(f"{requested}-")


class RetrievalMetricDeltas(StrictModel):
    """Aggregate-only evidence that is safe to send to the planner model."""

    recall_union_coverage: FiniteDelta
    fusion_recall_at_10: FiniteDelta
    fusion_ndcg_at_10: FiniteDelta
    fusion_mrr_at_10: FiniteDelta
    coarse_recall_at_10: FiniteDelta
    coarse_ndcg_at_10: FiniteDelta
    coarse_mrr_at_10: FiniteDelta


class RetrievalRiskSummary(StrictModel):
    """Bounded risk aggregates; never Query or product evidence."""

    unique_relevant_contribution: StrictInt = Field(ge=0, le=10_000)
    worst_coarse_query_ndcg_at_10_delta: FiniteDelta
    coarse_regressed_query_rate: UnitFloat
    worst_fusion_query_ndcg_at_10_delta: FiniteDelta
    fusion_regressed_query_rate: UnitFloat


class RetrievalDecisionObservation(StrictModel):
    """One explicitly projected observation for an LLM decision.

    The shape intentionally has no generic payload, Query text, product fields,
    file paths or free-form rationale.
    """

    sequence: StrictInt = Field(ge=1, le=6)
    option_id: RetrievalActionOptionId
    status: Literal["succeeded", "failed"]
    pipeline_variant: RetrievalPipelineVariant | None = None
    diagnosis_status: (
        Literal[
            "diagnosable",
            "no_failure",
            "insufficient_evidence",
            "requires_engineering",
        ]
        | None
    ) = None
    primary_category: (
        Literal[
            "recall",
            "fusion",
            "coarse_rank",
            "post_retrieval_ranking",
            "data_or_labels",
        ]
        | None
    ) = None
    gate_passed: StrictBool | None = None
    failed_gates: list[RetrievalGateName] = Field(default_factory=list, max_length=12)
    aggregate_deltas: RetrievalMetricDeltas | None = None
    risk: RetrievalRiskSummary | None = None
    error_code: StrictStr | None = Field(
        default=None,
        pattern=SAFE_ID_FIELD_PATTERN,
    )

    @model_validator(mode="after")
    def validate_observation_shape(self) -> Self:
        is_baseline = self.option_id == "diagnose_baseline"
        if is_baseline != (self.pipeline_variant is None):
            raise ValueError("pipeline variant does not match the observed option")
        if self.status == "failed":
            if (
                self.error_code is None
                or self.gate_passed is not None
                or self.failed_gates
                or self.aggregate_deltas is not None
                or self.risk is not None
                or self.diagnosis_status is not None
                or self.primary_category is not None
            ):
                raise ValueError("failed LLM observation contains evidence")
            return self
        if self.error_code is not None:
            raise ValueError("successful LLM observation contains an error")
        if is_baseline:
            if (
                self.diagnosis_status is None
                or self.gate_passed is not None
                or self.failed_gates
                or self.aggregate_deltas is not None
                or self.risk is not None
            ):
                raise ValueError("baseline LLM observation shape is invalid")
            return self
        if (
            self.diagnosis_status is None
            or self.gate_passed is None
            or self.aggregate_deltas is None
            or self.risk is None
            or self.gate_passed is not (not self.failed_gates)
        ):
            raise ValueError("candidate LLM observation shape is invalid")
        return self


class RetrievalDecisionContext(StrictModel):
    """Small aggregate-only view of one bounded Runtime step."""

    profile: Literal["smoke"] = "smoke"
    objective: Literal["expand_recall_without_downstream_regression"] = (
        "expand_recall_without_downstream_regression"
    )
    steps_used: StrictInt = Field(ge=0, le=8)
    tool_calls_used: StrictInt = Field(ge=0, le=6)
    remaining_steps: StrictInt = Field(ge=1, le=8)
    remaining_tool_calls: StrictInt = Field(ge=0, le=6)
    observations: list[RetrievalDecisionObservation] = Field(
        default_factory=list,
        max_length=6,
    )

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        if self.tool_calls_used != len(self.observations):
            raise ValueError("tool call count does not match LLM observations")
        if [item.sequence for item in self.observations] != list(
            range(1, len(self.observations) + 1)
        ):
            raise ValueError("LLM observations must be contiguous")
        return self


class LLMDecisionRequest(StrictModel):
    """One provider-independent, privacy-safe retrieval decision request."""

    schema_version: Literal[REQUEST_SCHEMA_VERSION] = REQUEST_SCHEMA_VERSION
    provider: LLMProviderId = OPENAI_PROVIDER_ID
    model: StrictStr = Field(pattern=MODEL_ID_PATTERN)
    allowed_option_ids: list[RetrievalOptionId] = Field(min_length=1, max_length=6)
    context: RetrievalDecisionContext
    provider_timeout_ms: StrictInt = Field(
        default=DEFAULT_PROVIDER_TIMEOUT_MS,
        ge=100,
        le=60_000,
    )
    max_output_tokens: StrictInt = Field(default=128, ge=32, le=256)

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if len(self.allowed_option_ids) != len(set(self.allowed_option_ids)):
            raise ValueError("allowed LLM options must be unique")
        trusted_order = {
            value: index for index, value in enumerate(RETRIEVAL_OPTION_IDS)
        }
        if self.allowed_option_ids != sorted(
            self.allowed_option_ids,
            key=trusted_order.__getitem__,
        ):
            raise ValueError("allowed LLM options must use the trusted order")
        return self


class LLMTokenUsage(StrictModel):
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)
    total_tokens: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total token usage is inconsistent")
        return self


class LLMDecisionResult(StrictModel):
    """Only metadata allowed to leave the isolated provider worker."""

    option_id: RetrievalOptionId
    provider: LLMProviderId = OPENAI_PROVIDER_ID
    model: StrictStr = Field(pattern=MODEL_ID_PATTERN)
    token_usage: LLMTokenUsage
    response_id_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    duration_ms: PositiveDuration
    attempt: Literal[1] = 1


LLMProviderErrorCode: TypeAlias = Literal[
    "llm_configuration_missing",
    "llm_request_too_large",
    "llm_worker_timeout",
    "llm_worker_response_too_large",
    "llm_worker_failed",
    "llm_worker_invalid_response",
    "llm_provider_timeout",
    "llm_provider_rate_limited",
    "llm_provider_auth_failed",
    "llm_provider_request_rejected",
    "llm_provider_unavailable",
    "llm_provider_invalid_response",
    "llm_provider_response_status_invalid",
    "llm_provider_response_model_invalid",
    "llm_provider_response_model_mismatch",
    "llm_provider_response_output_invalid",
    "llm_provider_response_usage_invalid",
    "llm_provider_response_id_invalid",
    "llm_provider_invalid_decision",
]


class LLMWorkerSuccess(StrictModel):
    ok: Literal[True] = True
    result: LLMDecisionResult


class LLMWorkerFailure(StrictModel):
    ok: Literal[False] = False
    error_code: LLMProviderErrorCode


class LLMProviderError(RuntimeError):
    """Stable provider failure without third-party exception text."""

    def __init__(self, code: LLMProviderErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _IsolatedResponsesDecisionProvider:
    """Launch one fixed-provider, killable worker for each model decision."""

    worker_timeout_ms: int = DEFAULT_WORKER_TIMEOUT_MS
    terminate_grace_ms: int = DEFAULT_TERMINATE_GRACE_MS
    worker_module: str = "search_quality.agent.llm_worker"

    @property
    def provider_id(self) -> LLMProviderId:
        raise NotImplementedError

    def __post_init__(self) -> None:
        if (
            type(self.worker_timeout_ms) is not int
            or not 100 <= self.worker_timeout_ms <= 120_000
            or type(self.terminate_grace_ms) is not int
            or not 1 <= self.terminate_grace_ms <= 5_000
            or self.worker_module != "search_quality.agent.llm_worker"
        ):
            raise ValueError("invalid LLM worker policy")

    def decide(self, request: LLMDecisionRequest) -> LLMDecisionResult:
        request = LLMDecisionRequest.model_validate(request, strict=True)
        if request.provider != self.provider_id:
            raise ValueError("LLM request provider does not match provider boundary")
        if self.worker_timeout_ms <= request.provider_timeout_ms:
            raise ValueError("worker timeout must exceed provider timeout")
        try:
            secret = _read_api_key(self.provider_id)
        except LLMProviderError as exc:
            logger.error(
                "agent_llm_worker_failed",
                extra={
                    "duration_ms": 0.0,
                    "error_code": exc.code,
                    "model": request.model,
                    "provider": self.provider_id,
                },
            )
            raise
        encoded = _canonical_json_bytes(request.model_dump(mode="json"))
        if len(encoded) > MAX_WORKER_REQUEST_BYTES:
            raise LLMProviderError("llm_request_too_large")
        started = time.perf_counter()
        logger.debug(
            "agent_llm_worker_started",
            extra={
                "model": request.model,
                "option_count": len(request.allowed_option_ids),
                "provider": self.provider_id,
                "request_bytes": len(encoded),
                "thinking_mode": (
                    "disabled"
                    if self.provider_id == VOLCENGINE_AGENT_PLAN_PROVIDER_ID
                    else "provider_default"
                ),
            },
        )
        try:
            raw_response = self._invoke_worker(encoded, secret)
            envelope = _parse_worker_envelope(raw_response)
            if isinstance(envelope, LLMWorkerFailure):
                raise LLMProviderError(envelope.error_code)
            result = envelope.result
            if (
                result.provider != self.provider_id
                or not provider_model_matches(
                    self.provider_id,
                    request.model,
                    result.model,
                )
                or result.option_id not in request.allowed_option_ids
            ):
                raise LLMProviderError("llm_worker_invalid_response")
        except LLMProviderError as exc:
            logger.error(
                "agent_llm_worker_failed",
                extra={
                    "duration_ms": round(_elapsed_ms(started), 3),
                    "error_code": exc.code,
                    "model": request.model,
                    "provider": self.provider_id,
                },
            )
            raise
        logger.debug(
            "agent_llm_worker_completed",
            extra={
                "attempt": result.attempt,
                "duration_ms": round(_elapsed_ms(started), 3),
                "input_token_count": result.token_usage.input_tokens,
                "model": result.model,
                "option_id": result.option_id,
                "output_token_count": result.token_usage.output_tokens,
                "provider": result.provider,
                "total_token_count": result.token_usage.total_tokens,
            },
        )
        return result

    def _invoke_worker(self, encoded: bytes, secret: str) -> bytes:
        child_env = {
            "PYTHONUNBUFFERED": "1",
            _worker_key_env_name(self.provider_id): secret,
        }
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", self.worker_module],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_env,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise LLMProviderError("llm_worker_failed") from None
        if process.stdin is None or process.stdout is None:
            _stop_worker(process, self.terminate_grace_ms)
            raise LLMProviderError("llm_worker_failed")
        try:
            process.stdin.write(encoded)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            _stop_worker(process, self.terminate_grace_ms)
            raise LLMProviderError("llm_worker_failed") from None

        deadline = time.monotonic() + self.worker_timeout_ms / 1000.0
        chunks: list[bytes] = []
        size = 0
        eof = False
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    _stop_worker(process, self.terminate_grace_ms)
                    raise LLMProviderError("llm_worker_timeout")
                events = selector.select(min(remaining, 0.05))
                if not events:
                    if process.poll() is not None:
                        # A closed process should make the pipe readable at EOF.
                        events = selector.select(0)
                        if not events:
                            eof = True
                    continue
                for key, _mask in events:
                    try:
                        chunk = os.read(key.fd, 4096)
                    except OSError:
                        _stop_worker(process, self.terminate_grace_ms)
                        raise LLMProviderError("llm_worker_failed") from None
                    if not chunk:
                        eof = True
                        break
                    size += len(chunk)
                    if size > MAX_WORKER_RESPONSE_BYTES:
                        _stop_worker(process, self.terminate_grace_ms)
                        raise LLMProviderError("llm_worker_response_too_large")
                    chunks.append(chunk)
        finally:
            selector.close()
            process.stdout.close()
        remaining = max(0.001, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _stop_worker(process, self.terminate_grace_ms)
            raise LLMProviderError("llm_worker_timeout") from None
        if return_code != 0:
            raise LLMProviderError("llm_worker_failed")
        return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class OpenAIResponsesDecisionProvider(_IsolatedResponsesDecisionProvider):
    """Official OpenAI Responses provider with a fixed worker boundary."""

    @property
    def provider_id(self) -> LLMProviderId:
        return OPENAI_PROVIDER_ID


@dataclass(frozen=True, slots=True)
class VolcengineAgentPlanDecisionProvider(_IsolatedResponsesDecisionProvider):
    """Volcengine Agent Plan provider with a fixed first-party endpoint."""

    @property
    def provider_id(self) -> LLMProviderId:
        return VOLCENGINE_AGENT_PLAN_PROVIDER_ID


def _worker_key_env_name(provider_id: LLMProviderId) -> str:
    if provider_id == OPENAI_PROVIDER_ID:
        # Preserve the existing disposable-worker contract for OpenAI.
        return "SEARCH_LLM_API_KEY"
    if provider_id == VOLCENGINE_AGENT_PLAN_PROVIDER_ID:
        return "SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY"
    raise ValueError("unsupported LLM provider")


def _read_api_key(provider_id: LLMProviderId) -> str:
    if provider_id == OPENAI_PROVIDER_ID:
        value = os.environ.get("SEARCH_OPENAI_API_KEY")
        if value is None:
            value = os.environ.get("SEARCH_LLM_API_KEY")
    elif provider_id == VOLCENGINE_AGENT_PLAN_PROVIDER_ID:
        value = os.environ.get("SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY")
    else:
        raise LLMProviderError("llm_configuration_missing")
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise LLMProviderError("llm_configuration_missing")
    return value


def _stop_worker(process: Any, grace_ms: int) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=grace_ms / 1000.0)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=grace_ms / 1000.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _parse_worker_envelope(raw: bytes) -> LLMWorkerSuccess | LLMWorkerFailure:
    try:
        value = _strict_json_loads(raw)
        if not isinstance(value, dict):
            raise ValueError("worker response is not an object")
        if value.get("ok") is True:
            return LLMWorkerSuccess.model_validate(value, strict=True)
        if value.get("ok") is False:
            return LLMWorkerFailure.model_validate(value, strict=True)
    except (TypeError, ValueError):
        pass
    raise LLMProviderError("llm_worker_invalid_response")


def _strict_json_loads(raw: bytes | str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(
        raw,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def response_id_sha256(response_id: str) -> str:
    """Hash a provider receipt before it crosses the worker boundary."""

    if not isinstance(response_id, str) or not response_id:
        raise ValueError("provider response ID is unavailable")
    return hashlib.sha256(response_id.encode("utf-8")).hexdigest()


def worker_command() -> tuple[str, str, str]:
    """Return the fixed command for diagnostics and tests; it contains no secret."""

    return (sys.executable, "-m", "search_quality.agent.llm_worker")


__all__ = [
    "DECISION_FUNCTION_NAME",
    "DEFAULT_PROVIDER_TIMEOUT_MS",
    "DEFAULT_WORKER_TIMEOUT_MS",
    "LLMDecisionRequest",
    "LLMDecisionResult",
    "LLMProviderId",
    "LLMProviderError",
    "LLMProviderErrorCode",
    "LLMTokenUsage",
    "LLMWorkerFailure",
    "LLMWorkerSuccess",
    "MAX_WORKER_REQUEST_BYTES",
    "MAX_WORKER_RESPONSE_BYTES",
    "OpenAIResponsesDecisionProvider",
    "OPENAI_PROVIDER_ID",
    "PROVIDER_ID",
    "REQUEST_SCHEMA_VERSION",
    "RETRIEVAL_OPTION_IDS",
    "RetrievalDecisionContext",
    "RetrievalDecisionObservation",
    "RetrievalMetricDeltas",
    "RetrievalOptionId",
    "RetrievalRiskSummary",
    "VOLCENGINE_AGENT_PLAN_PROVIDER_ID",
    "VolcengineAgentPlanDecisionProvider",
    "WORKER_LIFECYCLE_MARGIN_MS",
    "response_id_sha256",
    "worker_command",
]
