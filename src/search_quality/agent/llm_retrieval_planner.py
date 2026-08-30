"""Bounded LLM Planner for the retrieval optimization loop.

The model sees only aggregate, schema-validated observations and chooses one
server-generated option ID. The host maps that ID back to the canonical
``ToolAction`` or ``FinishDecision``; model output never becomes tool arguments.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from .contracts import (
    AgentDecision,
    PlannerDecisionAudit,
    RetrievalOptimizationTask,
)
from .llm_provider import (
    DEFAULT_PROVIDER_TIMEOUT_MS,
    DEFAULT_WORKER_TIMEOUT_MS,
    MODEL_ID_PATTERN,
    OPENAI_PROVIDER_ID,
    RETRIEVAL_OPTION_IDS,
    VOLCENGINE_AGENT_PLAN_PROVIDER_ID,
    WORKER_LIFECYCLE_MARGIN_MS,
    LLMDecisionRequest,
    LLMDecisionResult,
    LLMProviderId,
    OpenAIResponsesDecisionProvider,
    RetrievalDecisionContext,
    RetrievalDecisionObservation,
    RetrievalMetricDeltas,
    RetrievalOptionId,
    RetrievalRiskSummary,
    VolcengineAgentPlanDecisionProvider,
)
from .planner import PlannerView
from .retrieval_policy import (
    DIAGNOSE_BASELINE_OPTION,
    OPTION_BY_VARIANT,
    derive_adaptive_retrieval_options,
)
from .retrieval_tools import (
    DIAGNOSE_BASELINE_TOOL,
    RUN_CANDIDATE_TOOL,
    BaselineDiagnosisOutput,
    CandidateExperimentOutput,
)

logger = logging.getLogger("search_quality.agent_model")

PLANNER_ID = "llm-retrieval-planner-v1"
PROMPT_VERSION = "retrieval-choice-prompt-v1"
DECISION_SCHEMA_VERSION = "retrieval-choice-schema-v1"
DATA_POLICY = "aggregate_only_v1"
DEFAULT_MAX_OUTPUT_TOKENS = 128
DEFAULT_MAX_MODEL_CALLS = 6
DEFAULT_MAX_TOTAL_INPUT_TOKENS = 20_000
DEFAULT_MAX_TOTAL_OUTPUT_TOKENS = 1_024
RUNTIME_MAX_STEPS = 6
RUNTIME_MAX_TOOL_CALLS = 4
CONFIGURED_MODEL_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"


class DecisionProvider(Protocol):
    def decide(self, request: LLMDecisionRequest) -> LLMDecisionResult:
        """Return exactly one option selected from the request allowlist."""


class LLMPlannerConfigurationError(RuntimeError):
    """Stable configuration failure that contains no secret value."""


class LLMPlannerBudgetError(RuntimeError):
    """Raised before a tool call when the model-call budget is exhausted."""


@dataclass(frozen=True, slots=True)
class RetrievalPlannerConfiguration:
    planner_mode: Literal["deterministic", "llm"]
    state: Literal["deterministic", "ready", "not_configured"]
    model_id: str | None
    provider_timeout_ms: int
    worker_timeout_ms: int
    max_output_tokens: int
    key_configured: bool
    llm_provider_id: LLMProviderId | None

    @property
    def planner_id(self) -> str:
        if self.planner_mode == "llm":
            return PLANNER_ID
        return "stage-aware-retrieval-planner-v1"

    @property
    def provider_id(self) -> str | None:
        return self.llm_provider_id if self.planner_mode == "llm" else None


class LLMRetrievalPlanner:
    """Use one strict provider call for each bounded Runtime decision."""

    planner_id = PLANNER_ID
    decision_policy = "adaptive_llm_v1"
    requires_decision_audit = True

    def __init__(
        self,
        *,
        model_id: str,
        provider: DecisionProvider,
        provider_id: LLMProviderId = OPENAI_PROVIDER_ID,
        provider_timeout_ms: int = DEFAULT_PROVIDER_TIMEOUT_MS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
        max_total_input_tokens: int = DEFAULT_MAX_TOTAL_INPUT_TOKENS,
        max_total_output_tokens: int = DEFAULT_MAX_TOTAL_OUTPUT_TOKENS,
    ) -> None:
        if re.fullmatch(MODEL_ID_PATTERN, model_id) is None:
            raise ValueError("invalid LLM model identifier")
        if provider_id not in {
            OPENAI_PROVIDER_ID,
            VOLCENGINE_AGENT_PLAN_PROVIDER_ID,
        }:
            raise ValueError("invalid LLM provider identifier")
        limits = (
            provider_timeout_ms,
            max_output_tokens,
            max_model_calls,
            max_total_input_tokens,
            max_total_output_tokens,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("invalid LLM Planner budget")
        if not 100 <= provider_timeout_ms <= 60_000:
            raise ValueError("invalid LLM provider timeout")
        if not 32 <= max_output_tokens <= 256:
            raise ValueError("invalid LLM output-token limit")
        if max_model_calls > RUNTIME_MAX_STEPS:
            raise ValueError("LLM model-call budget exceeds Runtime steps")
        self.model_id = model_id
        self.provider = provider
        self.provider_id = provider_id
        self.provider_timeout_ms = provider_timeout_ms
        self.max_output_tokens = max_output_tokens
        self.max_model_calls = max_model_calls
        self.max_total_input_tokens = max_total_input_tokens
        self.max_total_output_tokens = max_total_output_tokens
        self._model_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._last_audit: PlannerDecisionAudit | None = None
        self._config_sha256 = _configuration_sha256(
            model_id=model_id,
            provider_id=provider_id,
            provider_timeout_ms=provider_timeout_ms,
            max_output_tokens=max_output_tokens,
            max_model_calls=max_model_calls,
            max_total_input_tokens=max_total_input_tokens,
            max_total_output_tokens=max_total_output_tokens,
        )

    def decide(self, view: PlannerView) -> AgentDecision:
        if not isinstance(view.task, RetrievalOptimizationTask):
            raise TypeError("LLM retrieval Planner requires a retrieval task")
        if view.task.decision_policy != self.decision_policy:
            raise ValueError("LLM retrieval task has the wrong decision policy")
        if self._last_audit is not None:
            raise ValueError("previous LLM Planner audit was not consumed")
        if self._model_calls >= self.max_model_calls:
            raise LLMPlannerBudgetError("llm_model_call_budget_exhausted")
        hard_call_ceiling = getattr(
            self.provider,
            "worker_timeout_ms",
            self.provider_timeout_ms + WORKER_LIFECYCLE_MARGIN_MS,
        )
        if (
            type(hard_call_ceiling) is not int
            or hard_call_ceiling <= self.provider_timeout_ms
        ):
            raise ValueError("LLM provider has no valid hard deadline")
        if view.remaining_ms <= hard_call_ceiling + 50:
            raise LLMPlannerBudgetError("llm_elapsed_budget_exhausted")

        options = derive_adaptive_retrieval_options(view.task, view.observations)
        if not options:
            raise ValueError("LLM retrieval Planner has no safe option")
        by_id = {option.option_id: option for option in options}
        allowed = [
            cast(RetrievalOptionId, option_id)
            for option_id in RETRIEVAL_OPTION_IDS
            if option_id in by_id
        ]
        if len(allowed) != len(options):
            raise ValueError("LLM retrieval option set is not trusted")

        request = LLMDecisionRequest(
            provider=self.provider_id,
            model=self.model_id,
            allowed_option_ids=allowed,
            context=_project_context(view),
            provider_timeout_ms=self.provider_timeout_ms,
            max_output_tokens=self.max_output_tokens,
        )
        result = self.provider.decide(request)
        if result.provider != self.provider_id:
            raise ValueError("LLM result provider does not match Planner configuration")
        self._model_calls += 1
        projected_input = self._input_tokens + result.token_usage.input_tokens
        projected_output = self._output_tokens + result.token_usage.output_tokens
        if (
            projected_input > self.max_total_input_tokens
            or projected_output > self.max_total_output_tokens
        ):
            logger.error(
                "agent_llm_budget_exhausted",
                extra={
                    "input_token_count": projected_input,
                    "model": self.model_id,
                    "model_call_count": self._model_calls,
                    "output_token_count": projected_output,
                },
            )
            raise LLMPlannerBudgetError("llm_token_budget_exhausted")
        self._input_tokens = projected_input
        self._output_tokens = projected_output

        option = by_id.get(result.option_id)
        if option is None:
            raise ValueError("LLM selected an option outside the current policy")
        self._last_audit = PlannerDecisionAudit(
            provider_id=result.provider,
            model_id=result.model,
            planner_config_sha256=self._config_sha256,
            response_id_sha256=result.response_id_sha256,
            selected_option_id=option.option_id,
            option_count=len(options),
            input_tokens=result.token_usage.input_tokens,
            output_tokens=result.token_usage.output_tokens,
            total_tokens=(
                result.token_usage.input_tokens + result.token_usage.output_tokens
            ),
            duration_ms=float(result.duration_ms),
        )
        logger.info(
            "agent_llm_decision_selected",
            extra={
                "model": self.model_id,
                "model_call_count": self._model_calls,
                "option_count": len(options),
                "option_id": option.option_id,
                "provider": self.provider_id,
            },
        )
        return option.decision

    def take_last_audit(self) -> PlannerDecisionAudit | None:
        audit = self._last_audit
        self._last_audit = None
        return audit


def load_retrieval_planner_configuration(
    environ: Mapping[str, str] | None = None,
) -> RetrievalPlannerConfiguration:
    values = os.environ if environ is None else environ
    raw_mode = values.get("SEARCH_AGENT_PLANNER", "deterministic")
    if raw_mode == "deterministic":
        return RetrievalPlannerConfiguration(
            planner_mode="deterministic",
            state="deterministic",
            model_id=None,
            provider_timeout_ms=DEFAULT_PROVIDER_TIMEOUT_MS,
            worker_timeout_ms=DEFAULT_WORKER_TIMEOUT_MS,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            key_configured=False,
            llm_provider_id=None,
        )
    if raw_mode not in {"llm", "openai"}:
        raise LLMPlannerConfigurationError("unsupported_llm_planner_mode")

    raw_provider = values.get("SEARCH_LLM_PROVIDER")
    if raw_mode == "openai":
        if raw_provider not in {None, OPENAI_PROVIDER_ID}:
            raise LLMPlannerConfigurationError("conflicting_llm_provider")
        provider_id: LLMProviderId = OPENAI_PROVIDER_ID
    else:
        if raw_provider not in {
            OPENAI_PROVIDER_ID,
            VOLCENGINE_AGENT_PLAN_PROVIDER_ID,
        }:
            raise LLMPlannerConfigurationError("unsupported_llm_provider")
        provider_id = cast(LLMProviderId, raw_provider)

    model_id = values.get("SEARCH_LLM_MODEL")
    key_configured = _valid_api_key(_configured_api_key(values, provider_id))
    timeout_ms = _bounded_int(
        values.get("SEARCH_LLM_TIMEOUT_MS"),
        default=DEFAULT_PROVIDER_TIMEOUT_MS,
        minimum=100,
        maximum=60_000,
        error_code="invalid_llm_timeout",
    )
    max_output_tokens = _bounded_int(
        values.get("SEARCH_LLM_MAX_OUTPUT_TOKENS"),
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        minimum=32,
        maximum=256,
        error_code="invalid_llm_output_budget",
    )
    model_valid = isinstance(model_id, str) and (
        re.fullmatch(CONFIGURED_MODEL_ID_PATTERN, model_id) is not None
    )
    return RetrievalPlannerConfiguration(
        planner_mode="llm",
        state="ready" if model_valid and key_configured else "not_configured",
        model_id=model_id if model_valid else None,
        provider_timeout_ms=timeout_ms,
        worker_timeout_ms=min(
            120_000,
            timeout_ms + WORKER_LIFECYCLE_MARGIN_MS,
        ),
        max_output_tokens=max_output_tokens,
        key_configured=key_configured,
        llm_provider_id=provider_id,
    )


def build_retrieval_planner(config: RetrievalPlannerConfiguration):
    if config.planner_mode == "deterministic":
        from .retrieval_planner import ObservationDrivenRetrievalPlanner

        return ObservationDrivenRetrievalPlanner()
    if config.state != "ready" or config.model_id is None or not config.key_configured:
        raise LLMPlannerConfigurationError("llm_planner_not_configured")
    if config.provider_id == OPENAI_PROVIDER_ID:
        provider: DecisionProvider = OpenAIResponsesDecisionProvider(
            worker_timeout_ms=config.worker_timeout_ms,
        )
    elif config.provider_id == VOLCENGINE_AGENT_PLAN_PROVIDER_ID:
        provider = VolcengineAgentPlanDecisionProvider(
            worker_timeout_ms=config.worker_timeout_ms,
        )
    else:
        raise LLMPlannerConfigurationError("unsupported_llm_provider")
    return LLMRetrievalPlanner(
        model_id=config.model_id,
        provider=provider,
        provider_id=config.provider_id,
        provider_timeout_ms=config.provider_timeout_ms,
        max_output_tokens=config.max_output_tokens,
    )


def _project_context(view: PlannerView) -> RetrievalDecisionContext:
    projected: list[RetrievalDecisionObservation] = []
    for sequence, observation in enumerate(view.observations, start=1):
        if observation.status == "failed":
            if observation.tool_name != DIAGNOSE_BASELINE_TOOL:
                raise ValueError("failed candidate cannot continue into an LLM turn")
            projected.append(
                RetrievalDecisionObservation(
                    sequence=sequence,
                    option_id=DIAGNOSE_BASELINE_OPTION,
                    status="failed",
                    error_code=observation.error_code,
                )
            )
            continue
        if observation.tool_name == DIAGNOSE_BASELINE_TOOL:
            envelope = BaselineDiagnosisOutput.model_validate(
                {
                    "evidence_ref": observation.evidence_ref,
                    "payload": observation.payload,
                },
                strict=True,
            )
            projected.append(
                RetrievalDecisionObservation(
                    sequence=sequence,
                    option_id=DIAGNOSE_BASELINE_OPTION,
                    status="succeeded",
                    diagnosis_status=envelope.payload.diagnosis_status,
                    primary_category=envelope.payload.primary_category,
                )
            )
            continue
        if observation.tool_name != RUN_CANDIDATE_TOOL:
            raise ValueError("LLM context contains an unsupported observation")
        envelope = CandidateExperimentOutput.model_validate(
            {
                "evidence_ref": observation.evidence_ref,
                "payload": observation.payload,
            },
            strict=True,
        )
        item = envelope.payload
        projected.append(
            RetrievalDecisionObservation(
                sequence=sequence,
                option_id=cast(
                    RetrievalOptionId,
                    OPTION_BY_VARIANT[item.pipeline_variant],
                ),
                status="succeeded",
                pipeline_variant=item.pipeline_variant,
                diagnosis_status=item.diagnosis_status,
                gate_passed=item.gate.passed,
                failed_gates=list(item.gate.failed_gates),
                aggregate_deltas=RetrievalMetricDeltas(
                    recall_union_coverage=item.aggregate_deltas.recall_union_coverage,
                    fusion_recall_at_10=(
                        item.aggregate_deltas.fusion.judged_recall_at_10
                    ),
                    fusion_ndcg_at_10=item.aggregate_deltas.fusion.ndcg_at_10,
                    fusion_mrr_at_10=item.aggregate_deltas.fusion.mrr_at_10,
                    coarse_recall_at_10=(
                        item.aggregate_deltas.coarse_rank.judged_recall_at_10
                    ),
                    coarse_ndcg_at_10=item.aggregate_deltas.coarse_rank.ndcg_at_10,
                    coarse_mrr_at_10=item.aggregate_deltas.coarse_rank.mrr_at_10,
                ),
                risk=RetrievalRiskSummary(
                    unique_relevant_contribution=(
                        item.risk.unique_relevant_contribution
                    ),
                    worst_coarse_query_ndcg_at_10_delta=(
                        item.risk.worst_coarse_query_ndcg_at_10_delta
                    ),
                    coarse_regressed_query_rate=(item.risk.coarse_regressed_query_rate),
                    worst_fusion_query_ndcg_at_10_delta=(
                        item.risk.worst_fusion_query_ndcg_at_10_delta
                    ),
                    fusion_regressed_query_rate=(item.risk.fusion_regressed_query_rate),
                ),
            )
        )
    return RetrievalDecisionContext(
        steps_used=view.steps_used,
        tool_calls_used=view.tool_calls_used,
        remaining_steps=max(1, RUNTIME_MAX_STEPS - view.steps_used),
        remaining_tool_calls=max(0, RUNTIME_MAX_TOOL_CALLS - view.tool_calls_used),
        observations=projected,
    )


def _configuration_sha256(**values: object) -> str:
    payload = {
        "data_policy": DATA_POLICY,
        "decision_schema_version": DECISION_SCHEMA_VERSION,
        "planner_id": PLANNER_ID,
        "prompt_version": PROMPT_VERSION,
        **values,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_int(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    error_code: str,
) -> int:
    if value is None:
        return default
    if not value.isascii() or not value.isdigit():
        raise LLMPlannerConfigurationError(error_code)
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise LLMPlannerConfigurationError(error_code)
    return parsed


def _valid_api_key(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip()
        and len(value) <= 512
        and all(ord(character) >= 33 and ord(character) != 127 for character in value)
    )


def _configured_api_key(
    values: Mapping[str, str],
    provider_id: LLMProviderId,
) -> str | None:
    if provider_id == VOLCENGINE_AGENT_PLAN_PROVIDER_ID:
        return values.get("SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY")
    preferred = values.get("SEARCH_OPENAI_API_KEY")
    return preferred if preferred is not None else values.get("SEARCH_LLM_API_KEY")


__all__ = [
    "LLMPlannerBudgetError",
    "LLMPlannerConfigurationError",
    "LLMRetrievalPlanner",
    "RetrievalPlannerConfiguration",
    "build_retrieval_planner",
    "load_retrieval_planner_configuration",
]
