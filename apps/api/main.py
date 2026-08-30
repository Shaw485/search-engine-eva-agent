"""Small Stage 0 API exposing the same smoke contract as the CLI."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from functools import lru_cache
from hashlib import sha256
from hmac import compare_digest
from hmac import new as hmac_new
from pathlib import Path
from typing import Literal, Self

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

from search_quality.agent.llm_retrieval_planner import (
    LLMPlannerConfigurationError,
    build_retrieval_planner,
    load_retrieval_planner_configuration,
)
from search_quality.agent.optimization import (
    ActiveStrategyChangedError,
    StrategyProposalRejectedError,
    apply_strategy_decision,
    generate_strategy_proposal,
    load_strategy_catalog,
)
from search_quality.agent.retrieval_runtime import generate_retrieval_runtime_analysis
from search_quality.agent_eval.runner import run_agent_eval_suite
from search_quality.bad_cases.artifacts import BadCaseRunInProgress
from search_quality.bad_cases.contracts import (
    BadCaseCategoryCounts,
    BadCaseDiagnosticArtifact,
)
from search_quality.bad_cases.supervisor import (
    DEFAULT_KILL_GRACE_MS,
    DEFAULT_TERM_GRACE_MS,
    DEFAULT_WORKER_DEADLINE_MS,
    BadCaseWorkerDeadlineExceeded,
    BadCaseWorkerError,
    load_supervisor_execution_receipt,
    supervise_bad_case_diagnostics,
)
from search_quality.catalog import (
    DEFAULT_CATALOG_INDEX,
    CatalogSearchService,
    InvalidCatalogQuery,
)
from search_quality.diagnostic_experiments import (
    DiagnosticExperimentPlan,
    load_diagnostic_artifacts,
    load_resolved_diagnostic_evidence,
    route_diagnostic_evidence,
)
from search_quality.evaluation.artifacts import require_clean_code_revision
from search_quality.human_oracle import (
    BehaviorJudgment,
    BehaviorReason,
    BehaviorSubmission,
    HumanOracleArtifact,
    HumanOracleRepository,
    IntentJudgment,
    IntentReason,
    IntentSubmission,
    OracleActor,
    OracleBatchArtifact,
    OracleBatchIncomplete,
    OracleBatchSealed,
    OracleBehaviorView,
    OracleClientActionConflict,
    OracleCompareAndSwapConflict,
    OracleIntentView,
    OracleInvalidDecision,
    OracleReviewState,
    OracleStorageError,
    SealSubmission,
    build_behavior_view,
    build_intent_view,
    build_oracle_batch,
    collect_behavior_samples_for_unit,
)
from search_quality.human_oracle.contracts import (
    BEHAVIOR_REASONS_BY_JUDGMENT,
    INTENT_REASONS_BY_JUDGMENT,
)
from search_quality.observability import (
    classify_error,
    configure_logging,
    current_trace_id,
    logging_context,
    new_trace_id,
)
from search_quality.query_constructor import build_smoke_query_set, store_query_set
from search_quality.query_constructor.contracts import QuerySetArtifact
from search_quality.smoke import run_smoke

configure_logging()
logger = logging.getLogger("search_quality.api")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CODE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_AGENT_PROPOSAL_LOCK = threading.Lock()
_AGENT_EVAL_LOCK = threading.Lock()
_RETRIEVAL_ANALYSIS_LOCK = threading.Lock()
_BAD_CASE_LOCK = threading.Lock()
_AGENT_PROPOSAL_CACHE: dict[tuple[str, str, str, str], dict] = {}

app = FastAPI(
    title="Search Engine EVA Agent",
    version="0.3.0",
    description="Full-catalog baseline search and evaluation service",
)


def _agent_artifact_root() -> Path | None:
    configured = os.environ.get("SEARCH_AGENT_ARTIFACT_ROOT")
    if not configured:
        return None
    requested = Path(configured)
    if not requested.is_absolute():
        raise RuntimeError("configured strategy artifact root is invalid")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("configured strategy artifact root is unavailable") from exc
    if not resolved.is_dir():
        raise RuntimeError("configured strategy artifact root is unavailable")
    return resolved


def _runtime_artifact_root() -> Path:
    """Return the single private evidence root used by owner-only workflows."""

    return _agent_artifact_root() or (PROJECT_ROOT / "runs")


def _api_code_revision(project_root: Path) -> str:
    configured = os.environ.get("SEARCH_CODE_REVISION")
    if not configured:
        return require_clean_code_revision(project_root)
    if not CODE_REVISION_PATTERN.fullmatch(configured):
        raise RuntimeError("configured code revision is invalid")
    return configured


def _cached_agent_strategy_proposal(*, profile_id: Literal["smoke"]) -> dict:
    artifact_root = _agent_artifact_root()
    configured_revision = os.environ.get("SEARCH_CODE_REVISION")
    if not configured_revision:
        for _attempt in range(2):
            try:
                return generate_strategy_proposal(
                    project_root=PROJECT_ROOT,
                    artifact_root=artifact_root,
                    profile_id=profile_id,
                )
            except ActiveStrategyChangedError:
                logger.info(
                    "agent_strategy_proposal_parent_changed",
                    extra={"profile_id": profile_id},
                )
        raise RuntimeError(
            "active strategy changed repeatedly during proposal generation"
        )
    revision = _api_code_revision(PROJECT_ROOT)
    for _attempt in range(2):
        with _AGENT_PROPOSAL_LOCK:
            parent_revision = load_strategy_catalog(
                project_root=PROJECT_ROOT,
                artifact_root=artifact_root,
            ).get("active_revision")
            cache_key = (
                profile_id,
                revision,
                str(artifact_root),
                str(parent_revision or "none"),
            )
            cached = _AGENT_PROPOSAL_CACHE.get(cache_key)
            if cached is not None:
                logger.debug(
                    "agent_strategy_proposal_cache_hit",
                    extra={"profile_id": profile_id},
                )
                return cached
            logger.debug(
                "agent_strategy_proposal_cache_miss",
                extra={"profile_id": profile_id},
            )
            try:
                proposal = generate_strategy_proposal(
                    project_root=PROJECT_ROOT,
                    artifact_root=artifact_root,
                    profile_id=profile_id,
                    revision_provider=lambda _root: revision,
                )
            except ActiveStrategyChangedError:
                logger.info(
                    "agent_strategy_proposal_parent_changed",
                    extra={"profile_id": profile_id},
                )
                continue
            current_revision = load_strategy_catalog(
                project_root=PROJECT_ROOT,
                artifact_root=artifact_root,
            ).get("active_revision")
            if (
                proposal.get("parent_active_strategy_revision") != parent_revision
                or current_revision != parent_revision
            ):
                logger.info(
                    "agent_strategy_proposal_parent_changed",
                    extra={"profile_id": profile_id},
                )
                continue
            _AGENT_PROPOSAL_CACHE[cache_key] = proposal
            return proposal
    raise RuntimeError("active strategy changed repeatedly during proposal generation")


def _is_local_owner_request(request: Request) -> bool:
    forwarded = any(
        request.headers.get(name)
        for name in ("forwarded", "x-forwarded-for", "x-real-ip")
    )
    client_host = request.client.host if request.client is not None else ""
    return not forwarded and client_host in {"127.0.0.1", "::1"}


# The production portfolio uses a same-origin Nginx proxy. These two origins are
# only for local visual QA when the static site and API run on separate ports.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:4173", "http://localhost:4173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Accept", "Content-Type"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_diagnostics(request: Request, call_next):
    """Correlate requests without logging query strings or request bodies."""

    # Generate correlation IDs inside the trust boundary. An arbitrary incoming
    # X-Request-ID may itself contain a credential and must never enter logs.
    trace_id = new_trace_id()
    started = time.perf_counter()
    safe_context = {
        "method": (
            request.method
            if request.method in {"GET", "HEAD", "OPTIONS", "POST"}
            else "OTHER"
        ),
        "route": (
            request.url.path
            if request.url.path
            in {
                "/agent/strategy/decision",
                "/agent/strategy/catalog",
                "/agent/strategy/propose",
                "/agent/retrieval/analyze",
                "/agent/retrieval/status",
                "/agent/eval/run",
                "/agent/bad-cases/run",
                "/agent/diagnostic-experiments/plan",
                "/agent/human-oracle/batches/create",
                "/agent/human-oracle/batches/seal",
                "/agent/human-oracle/batches/status",
                "/agent/human-oracle/behaviors/submit",
                "/agent/human-oracle/behaviors/view",
                "/agent/human-oracle/intents/submit",
                "/agent/human-oracle/intents/view",
                "/agent/query-constructor/build",
                "/catalog/search",
                "/health",
                "/smoke",
            }
            else "unmatched"
        ),
        "trace_id": trace_id,
    }
    with logging_context(**safe_context):
        logger.debug("request_started")
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "request_failed",
                extra={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                },
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "code": "internal_server_error",
                        "message": "Internal server error",
                        "trace_id": trace_id,
                    }
                },
            )
            response.headers["X-Request-ID"] = trace_id
            return response
        response.headers["X-Request-ID"] = trace_id
        logger.info(
            "request_completed",
            extra={
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "status_code": response.status_code,
            },
        )
        return response


def _catalog_index_path() -> Path:
    configured = os.environ.get("SEARCH_CATALOG_INDEX")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / DEFAULT_CATALOG_INDEX


@lru_cache(maxsize=4)
def _cached_catalog_service(index_path: str) -> CatalogSearchService:
    return CatalogSearchService(index_path)


def get_catalog_search_service() -> CatalogSearchService:
    return _cached_catalog_service(str(_catalog_index_path()))


@app.get("/health")
def health() -> dict:
    try:
        metadata = get_catalog_search_service().metadata
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        catalog = {"status": "unavailable"}
    else:
        catalog = {
            "index_id": metadata.index_id,
            "product_count": metadata.product_count,
            "status": "ready",
        }
    return {"catalog": catalog, "stage": "catalog-baseline", "status": "ok"}


class SmokeRequest(BaseModel):
    """Body-based request that keeps search text out of URLs."""

    query: str = Field(default="wireless mouse", min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=10)
    backend: Literal["local", "opensearch"] = "local"


class CatalogSearchRequest(BaseModel):
    """Full-catalog request kept in the HTTPS body rather than the URL."""

    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=10, ge=1, le=20)


class StrategyProposalRequest(BaseModel):
    """Start one approval-gated Agent strategy proposal run."""

    profile: Literal["smoke"] = "smoke"


class RetrievalAnalysisRequest(BaseModel):
    """Run the fixed smoke-only stage-aware retrieval analysis."""

    model_config = ConfigDict(extra="forbid", strict=True)

    profile: Literal["smoke"] = "smoke"


class AgentEvalRequest(BaseModel):
    """Run the fixed Stage 5 Agent Eval suite; arbitrary suites are forbidden."""

    model_config = ConfigDict(extra="forbid", strict=True)

    suite: Literal["stage5-retrieval-v1"] = "stage5-retrieval-v1"


class AgentEvalMetricsResponse(BaseModel):
    """Privacy-safe aggregate Agent behavior metrics."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_success_rate: float = Field(ge=0.0, le=1.0)
    grounded_claim_rate: float = Field(ge=0.0, le=1.0)
    tool_selection_accuracy: float = Field(ge=0.0, le=1.0)
    recovery_rate: float = Field(ge=0.0, le=1.0)
    budget_compliance_rate: float = Field(ge=0.0, le=1.0)
    replay_fidelity_rate: float = Field(ge=0.0, le=1.0)
    tamper_rejection_rate: float = Field(ge=0.0, le=1.0)
    unauthorized_effect_count: int = Field(ge=0)
    protected_profile_read_count: int = Field(ge=0)
    strategy_write_count: int = Field(ge=0)
    total_agent_steps: int = Field(ge=0)
    total_agent_tool_calls: int = Field(ge=0)
    comparable_workflow_success_rate: float = Field(ge=0.0, le=1.0)
    comparable_workflow_tool_calls: int = Field(ge=0)


class AgentEvalSubjectResponse(BaseModel):
    """Aggregate attribution without exposing task payloads or Trace details."""

    model_config = ConfigDict(extra="forbid", strict=True)

    subject_kind: Literal["production_planner", "harness_stimulus"]
    task_count: int = Field(ge=1, le=12)
    passed_count: int = Field(ge=0, le=12)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.passed_count > self.task_count:
            raise ValueError("subject pass count exceeds its task count")
        return self


class AgentEvalResponse(BaseModel):
    """Small response for the workbench; detailed traces stay server-side."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["agent-eval-api-summary-v1"]
    suite_id: Literal["stage5-retrieval-v1"]
    evidence_id: str = Field(pattern=r"^agent-eval-[0-9a-f]{12}$")
    execution_id: str = Field(pattern=r"^agent-eval-execution-[0-9a-f]{32}$")
    formal_passed: bool
    task_count: int = Field(ge=12, le=12)
    metrics: AgentEvalMetricsResponse
    subject_summaries: tuple[AgentEvalSubjectResponse, AgentEvalSubjectResponse]
    limitations: tuple[
        Literal["scripted_failures_do_not_prove_worker_deadline_enforcement"],
        Literal["contract_fixtures_test_runtime_behavior_not_search_quality"],
        Literal["grounded_claim_rate_v1_is_terminal_grounding_proxy"],
    ]

    @model_validator(mode="after")
    def validate_attribution(self) -> Self:
        if tuple(item.subject_kind for item in self.subject_summaries) != (
            "production_planner",
            "harness_stimulus",
        ):
            raise ValueError("Agent Eval subject summaries are missing or reordered")
        if tuple(item.task_count for item in self.subject_summaries) != (8, 4):
            raise ValueError("Agent Eval subject task counts do not match Suite v1")
        if sum(item.task_count for item in self.subject_summaries) != self.task_count:
            raise ValueError("Agent Eval subject task counts do not match the Suite")
        if self.metrics.total_agent_steps < self.metrics.total_agent_tool_calls:
            raise ValueError("Agent Eval tool calls exceed total Agent steps")
        passed = sum(item.passed_count for item in self.subject_summaries)
        if abs(self.metrics.task_success_rate - (passed / self.task_count)) > 1e-12:
            raise ValueError("Agent Eval task rate does not match subject summaries")
        formal_rates = (
            self.metrics.task_success_rate,
            self.metrics.grounded_claim_rate,
            self.metrics.tool_selection_accuracy,
            self.metrics.recovery_rate,
            self.metrics.budget_compliance_rate,
            self.metrics.replay_fidelity_rate,
            self.metrics.tamper_rejection_rate,
        )
        expected_formal = (
            all(rate == 1.0 for rate in formal_rates)
            and self.metrics.unauthorized_effect_count == 0
            and self.metrics.protected_profile_read_count == 0
            and self.metrics.strategy_write_count == 0
            and all(
                item.passed_count == item.task_count for item in self.subject_summaries
            )
        )
        if self.formal_passed != expected_formal:
            raise ValueError("formal Agent Eval does not match its aggregate evidence")
        return self


class QueryConstructorRequest(BaseModel):
    """Build only from the committed smoke source."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["smoke"] = "smoke"


class QueryConstructionCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    identity: int = Field(ge=0)
    adjacent_transposition: int = Field(ge=0)
    token_order_reversal: int = Field(ge=0)


class QueryConstructorResponse(BaseModel):
    """Aggregate Query-set metadata with no raw Query text or labels."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["query-constructor-api-summary-v1"]
    source: Literal["smoke"]
    query_set_id: str = Field(pattern=r"^query-set-[0-9a-f]{12}$")
    query_count: int = Field(ge=1)
    original_count: int = Field(ge=1)
    synthetic_count: int = Field(ge=0)
    deduplicated_count: int = Field(ge=0)
    construction_counts: QueryConstructionCountsResponse
    formal_evaluation_allowed: Literal[False]
    locked_profiles_not_read: tuple[Literal["dev"], Literal["test"]]
    cross_split_collision_status: Literal["not_checked_without_reading_locked_splits"]

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        counts = self.construction_counts
        if self.query_count != self.original_count + self.synthetic_count:
            raise ValueError(
                "Query count does not match original plus synthetic counts"
            )
        if counts.identity != self.original_count:
            raise ValueError("identity count does not match original count")
        if (
            counts.adjacent_transposition + counts.token_order_reversal
            != self.synthetic_count
        ):
            raise ValueError(
                "synthetic construction counts do not match synthetic count"
            )
        if (
            counts.identity
            + counts.adjacent_transposition
            + counts.token_order_reversal
            != self.query_count
        ):
            raise ValueError("construction counts do not match Query count")
        return self


class BadCaseRunRequest(BaseModel):
    """Run only the fixed source-bounded smoke Query set."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["smoke"] = "smoke"


class DiagnosticExperimentPlanRequest(BaseModel):
    """Plan from one immutable diagnostic pair, never from arbitrary paths."""

    model_config = ConfigDict(extra="forbid", strict=True)

    diagnostic_id: str = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    query_set_id: str = Field(pattern=r"^query-set-[0-9a-f]{12}$")


class HumanOracleBatchCreateRequest(BaseModel):
    """Create only from one fixed immutable diagnostic/Query-set pair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    diagnostic_id: str = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    query_set_id: str = Field(pattern=r"^query-set-[0-9a-f]{12}$")


class HumanOracleBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    oracle_batch_id: str = Field(pattern=r"^oracle-batch-[0-9a-f]{12}$")


class HumanOracleUnitRequest(HumanOracleBatchRequest):
    unit_id: str = Field(pattern=r"^oracle-unit-[0-9a-f]{12}$")


class HumanOracleIntentSubmitRequest(HumanOracleUnitRequest):
    """Owner input only; actor identity is injected by the server."""

    case_id: str = Field(pattern=r"^query-case-[0-9a-f]{12}$")
    presentation_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment: Literal["equivalent", "not_equivalent", "uncertain"]
    reason_code: Literal[
        "same_product_intent",
        "obvious_typo_same_intent",
        "meaning_changed",
        "query_became_uninterpretable",
        "ambiguous_intent",
        "insufficient_context",
    ]
    client_action_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    expected_previous_annotation_id: str | None = Field(
        default=None,
        pattern=r"^oracle-intent-[0-9a-f]{12}$",
    )

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        judgment = IntentJudgment(self.judgment)
        reason = IntentReason(self.reason_code)
        if reason not in INTENT_REASONS_BY_JUDGMENT[judgment]:
            raise ValueError("intent reason does not match its judgment")
        return self


class HumanOracleBehaviorSubmitRequest(HumanOracleUnitRequest):
    """Owner input only; evidence and actor identity cannot be uploaded."""

    case_id: str = Field(pattern=r"^query-case-[0-9a-f]{12}$")
    presentation_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment: Literal["confirmed_issue", "acceptable", "uncertain"]
    reason_code: Literal[
        "owner_catalog_expectation",
        "equivalent_intent_should_preserve_behavior",
        "intent_not_equivalent",
        "behavior_is_expected",
        "catalog_coverage_unknown",
        "insufficient_result_evidence",
        "insufficient_domain_knowledge",
    ]
    intent_annotation_id: str | None = Field(
        default=None,
        pattern=r"^oracle-intent-[0-9a-f]{12}$",
    )
    client_action_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    expected_previous_annotation_id: str | None = Field(
        default=None,
        pattern=r"^oracle-behavior-[0-9a-f]{12}$",
    )

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        judgment = BehaviorJudgment(self.judgment)
        reason = BehaviorReason(self.reason_code)
        if reason not in BEHAVIOR_REASONS_BY_JUDGMENT[judgment]:
            raise ValueError("behavior reason does not match its judgment")
        return self


class HumanOracleSealRequest(HumanOracleBatchRequest):
    client_action_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )


class HumanOracleUnitSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    unit_id: str = Field(pattern=r"^oracle-unit-[0-9a-f]{12}$")
    source_case_id: str = Field(pattern=r"^query-case-[0-9a-f]{12}$")
    stratum: Literal["source_zero_cluster", "source_nonzero_variant_zero"]
    candidate_count: int = Field(ge=1, le=3)


class HumanOracleBatchCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["human-oracle-batch-api-summary-v1"]
    oracle_batch_id: str = Field(pattern=r"^oracle-batch-[0-9a-f]{12}$")
    diagnostic_id: str = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    query_set_id: str = Field(pattern=r"^query-set-[0-9a-f]{12}$")
    selected_cluster_count: Literal[20]
    selected_candidate_count: Literal[40]
    synthetic_intent_candidate_count: Literal[30]
    units: list[HumanOracleUnitSummary] = Field(min_length=20, max_length=20)
    formal_evaluation_allowed: Literal[False]
    quality_conclusion_allowed: Literal[False]
    strategy_write_count: Literal[0]

    @model_validator(mode="after")
    def validate_units(self) -> Self:
        if len({item.unit_id for item in self.units}) != 20:
            raise ValueError("Human Oracle API unit IDs must be unique")
        if sum(item.candidate_count for item in self.units) != 40:
            raise ValueError("Human Oracle API candidate counts must total 40")
        return self


class HumanOracleIntentSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["human-oracle-intent-api-summary-v1"]
    intent_annotation_id: str = Field(pattern=r"^oracle-intent-[0-9a-f]{12}$")
    oracle_batch_id: str = Field(pattern=r"^oracle-batch-[0-9a-f]{12}$")
    unit_id: str = Field(pattern=r"^oracle-unit-[0-9a-f]{12}$")
    case_id: str = Field(pattern=r"^query-case-[0-9a-f]{12}$")
    judgment: IntentJudgment
    reason_code: IntentReason
    supersedes_annotation_id: str | None = Field(
        default=None,
        pattern=r"^oracle-intent-[0-9a-f]{12}$",
    )
    result_evidence_was_withheld: Literal[True]
    product_relevance_labels_created: Literal[0]


class HumanOracleBehaviorSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["human-oracle-behavior-api-summary-v1"]
    behavior_annotation_id: str = Field(pattern=r"^oracle-behavior-[0-9a-f]{12}$")
    oracle_batch_id: str = Field(pattern=r"^oracle-batch-[0-9a-f]{12}$")
    unit_id: str = Field(pattern=r"^oracle-unit-[0-9a-f]{12}$")
    case_id: str = Field(pattern=r"^query-case-[0-9a-f]{12}$")
    judgment: BehaviorJudgment
    reason_code: BehaviorReason
    intent_annotation_id: str | None = Field(
        default=None,
        pattern=r"^oracle-intent-[0-9a-f]{12}$",
    )
    supersedes_annotation_id: str | None = Field(
        default=None,
        pattern=r"^oracle-behavior-[0-9a-f]{12}$",
    )
    product_relevance_labels_created: Literal[0]
    root_cause_claimed: Literal[False]


class HumanOracleSealResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["human-oracle-seal-api-summary-v1"]
    oracle_id: str = Field(pattern=r"^human-oracle-[0-9a-f]{12}$")
    oracle_batch_id: str = Field(pattern=r"^oracle-batch-[0-9a-f]{12}$")
    diagnostic_id: str = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    synthetic_intent_annotation_count: Literal[30]
    behavior_annotation_count: Literal[40]
    product_relevance_labels_created: Literal[0]
    formal_evaluation_allowed: Literal[False]
    quality_conclusion_allowed: Literal[False]
    root_cause_claimed: Literal[False]
    strategy_write_count: Literal[0]
    limitations: tuple[
        Literal["single_owner_no_inter_annotator_agreement"],
        Literal["selection_conditioned_development_set"],
        Literal["synthetic_product_relevance_remains_unjudged"],
        Literal["prior_exposure_not_controlled"],
        Literal["diagnostic_judgment_is_not_root_cause"],
    ]


def _human_oracle_actor_from_request(request: Request) -> OracleActor:
    """Derive a pseudonymous actor only inside the authenticated proxy boundary."""

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        logger.warning(
            "human_oracle_request_rejected",
            extra={"error_code": "json_content_type_required"},
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "human_oracle_request_invalid",
                "message": "Human Oracle request is invalid",
                "trace_id": current_trace_id(),
            },
        )

    allowed_origin = os.environ.get("SEARCH_HUMAN_ORACLE_ALLOWED_ORIGIN")
    actor_key = os.environ.get("SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY")
    actor_key_id = os.environ.get("SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY_ID")
    allowed_owner_hmac = os.environ.get("SEARCH_HUMAN_ORACLE_OWNER_HMAC_SHA256")
    if (
        allowed_origin is None
        or re.fullmatch(
            r"https?://(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+(?::[0-9]{1,5})?",
            allowed_origin,
        )
        is None
        or actor_key is None
        or not 32 <= len(actor_key.encode("utf-8")) <= 512
        or actor_key_id is None
        or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", actor_key_id) is None
        or allowed_owner_hmac is None
        or re.fullmatch(r"[0-9a-f]{64}", allowed_owner_hmac) is None
    ):
        logger.error(
            "human_oracle_configuration_unavailable",
            extra={"error_code": "human_oracle_configuration_invalid"},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "human_oracle_unavailable",
                "message": "Human Oracle unavailable",
                "trace_id": current_trace_id(),
            },
        )

    if (
        request.headers.get("origin") != allowed_origin
        or request.headers.get("sec-fetch-site") != "same-origin"
    ):
        logger.warning(
            "human_oracle_request_rejected",
            extra={"error_code": "same_origin_required"},
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "human_oracle_request_invalid",
                "message": "Human Oracle request is invalid",
                "trace_id": current_trace_id(),
            },
        )

    principal = request.headers.get("x-search-owner-principal")
    if (
        principal is None
        or not 1 <= len(principal) <= 256
        or principal != principal.strip()
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in principal
        )
    ):
        logger.warning(
            "human_oracle_request_rejected",
            extra={"error_code": "owner_principal_required"},
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "human_oracle_request_invalid",
                "message": "Human Oracle request is invalid",
                "trace_id": current_trace_id(),
            },
        )
    principal_hmac = hmac_new(
        actor_key.encode("utf-8"),
        principal.encode("utf-8"),
        sha256,
    ).hexdigest()
    if not compare_digest(principal_hmac, allowed_owner_hmac):
        logger.warning(
            "human_oracle_request_rejected",
            extra={"error_code": "owner_principal_not_authorized"},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "human_oracle_owner_forbidden",
                "message": "Human Oracle access forbidden",
                "trace_id": current_trace_id(),
            },
        )
    return OracleActor(
        principal_hmac_sha256=principal_hmac,
        actor_key_id=actor_key_id,
    )


def _human_oracle_repository() -> HumanOracleRepository:
    return HumanOracleRepository(_runtime_artifact_root())


def _load_human_oracle_evidence(
    repository: HumanOracleRepository,
    oracle_batch_id: str,
) -> tuple[OracleBatchArtifact, BadCaseDiagnosticArtifact, QuerySetArtifact]:
    """Reload and rebuild the batch from the immutable evidence on every action."""

    batch = repository.load_batch(oracle_batch_id)
    diagnostic, query_set = load_diagnostic_artifacts(
        artifact_root=_runtime_artifact_root(),
        diagnostic_id=batch.diagnostic_id,
        query_set_id=batch.query_set_id,
    )
    if build_oracle_batch(diagnostic=diagnostic, query_set=query_set) != batch:
        raise ValueError("Human Oracle batch evidence changed")
    return batch, diagnostic, query_set


def _raise_human_oracle_conflict(
    *,
    operation: str,
    error: Exception,
    oracle_batch_id: str | None = None,
    unit_id: str | None = None,
) -> None:
    extra: dict[str, object] = {
        "error_code": "human_oracle_state_conflict",
        "error_type": type(error).__name__,
        "operation": operation,
    }
    if oracle_batch_id is not None and re.fullmatch(
        r"^oracle-batch-[0-9a-f]{12}$", oracle_batch_id
    ):
        extra["oracle_batch_id"] = oracle_batch_id
    if unit_id is not None and re.fullmatch(r"^oracle-unit-[0-9a-f]{12}$", unit_id):
        extra["unit_id"] = unit_id
    logger.warning("human_oracle_api_conflict", extra=extra)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "human_oracle_state_conflict",
            "message": "Human Oracle evidence or state changed",
            "trace_id": current_trace_id(),
        },
    ) from error


def _raise_human_oracle_invalid_decision(
    *,
    operation: str,
    error: Exception,
    oracle_batch_id: str,
    unit_id: str | None = None,
) -> None:
    extra: dict[str, object] = {
        "error_code": "human_oracle_decision_invalid",
        "error_type": type(error).__name__,
        "operation": operation,
        "oracle_batch_id": oracle_batch_id,
    }
    if unit_id is not None and re.fullmatch(r"^oracle-unit-[0-9a-f]{12}$", unit_id):
        extra["unit_id"] = unit_id
    logger.warning("human_oracle_api_decision_rejected", extra=extra)
    raise HTTPException(
        status_code=422,
        detail={
            "code": "human_oracle_decision_invalid",
            "message": "Human Oracle judgment is invalid",
            "trace_id": current_trace_id(),
        },
    ) from error


def _raise_human_oracle_unavailable(
    *,
    operation: str,
    error: Exception,
    oracle_batch_id: str | None = None,
) -> None:
    extra: dict[str, object] = {
        "error_code": "human_oracle_unavailable",
        "error_type": type(error).__name__,
        "operation": operation,
    }
    if oracle_batch_id is not None and re.fullmatch(
        r"^oracle-batch-[0-9a-f]{12}$", oracle_batch_id
    ):
        extra["oracle_batch_id"] = oracle_batch_id
    logger.error("human_oracle_api_failed", extra=extra)
    raise HTTPException(
        status_code=503,
        detail={
            "code": "human_oracle_unavailable",
            "message": "Human Oracle unavailable",
            "trace_id": current_trace_id(),
        },
    ) from error


def _get_human_oracle_catalog_service() -> CatalogSearchService:
    """Keep catalog startup failures distinct from stale evidence conflicts."""

    try:
        return get_catalog_search_service()
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        _raise_human_oracle_unavailable(
            operation="behavior_view_catalog_startup",
            error=exc,
        )


BadCaseCategoryName = Literal[
    "zero_result",
    "spelling_sensitive",
    "order_sensitive",
    "ranking_instability_needs_judgment",
]
BadCaseReasonName = Literal[
    "identity_zero_result",
    "variant_zero_result",
    "variant_result_set_changed",
    "variant_ranking_changed",
    "token_order_result_changed",
]
BAD_CASE_CATEGORY_ORDER = (
    "zero_result",
    "spelling_sensitive",
    "order_sensitive",
    "ranking_instability_needs_judgment",
)


class BadCaseDisplayHitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    product_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    locale: str = Field(pattern=r"^[a-z][a-z0-9-]{1,15}$")
    title: str = Field(min_length=1, max_length=256)
    rank: int = Field(ge=1, le=3)


class BadCaseSampleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    case_id: str = Field(pattern=r"^query-case-[0-9a-f]{12}$")
    source_case_id: str = Field(pattern=r"^query-case-[0-9a-f]{12}$")
    construction: Literal["identity", "adjacent_transposition", "token_order_reversal"]
    categories: list[BadCaseCategoryName] = Field(min_length=1, max_length=4)
    reason_code: BadCaseReasonName
    query_text: str = Field(min_length=1, max_length=200)
    source_query_text: str = Field(min_length=1, max_length=200)
    source_returned_at_k: int = Field(ge=0, le=10)
    variant_returned_at_k: int = Field(ge=0, le=10)
    overlap_at_k: int = Field(ge=0, le=10)
    source_top_hits: list[BadCaseDisplayHitResponse] = Field(max_length=3)
    variant_top_hits: list[BadCaseDisplayHitResponse] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_sample(self) -> Self:
        if self.categories != [
            category
            for category in BAD_CASE_CATEGORY_ORDER
            if category in self.categories
        ] or len(self.categories) != len(set(self.categories)):
            raise ValueError("sample categories must be unique and ordered")
        if self.overlap_at_k > min(
            self.source_returned_at_k,
            self.variant_returned_at_k,
        ):
            raise ValueError("sample overlap exceeds returned results")
        if self.construction == "identity":
            if (
                self.case_id != self.source_case_id
                or self.query_text != self.source_query_text
            ):
                raise ValueError("identity sample must match its source")
        elif self.case_id == self.source_case_id:
            raise ValueError("synthetic sample must reference its identity source")
        if "zero_result" in self.categories and self.variant_returned_at_k != 0:
            raise ValueError("zero-result sample must have no variant results")
        if "spelling_sensitive" in self.categories and (
            self.construction != "adjacent_transposition"
        ):
            raise ValueError("spelling sensitivity requires transposition")
        if "order_sensitive" in self.categories and (
            self.construction != "token_order_reversal"
        ):
            raise ValueError("order sensitivity requires token reversal")
        if "ranking_instability_needs_judgment" in self.categories and (
            self.source_returned_at_k == 0 or self.variant_returned_at_k == 0
        ):
            raise ValueError("ranking instability requires results on both sides")
        _validate_api_display_hits(self.source_top_hits, self.source_returned_at_k)
        _validate_api_display_hits(self.variant_top_hits, self.variant_returned_at_k)
        return self


class BadCaseRunResponse(BaseModel):
    """Owner-only aggregate plus strictly limited understandable samples."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["bad-case-api-summary-v2"]
    completed: Literal[True]
    diagnostic_id: str = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    execution_id: str = Field(pattern=r"^bad-case-execution-[0-9a-f]{32}$")
    query_set_id: str = Field(pattern=r"^query-set-[0-9a-f]{12}$")
    index_id: str = Field(pattern=r"^catalog-baseline-v1-[0-9a-f]{12}$")
    search_strategy_id: Literal["sqlite-fts5-bm25"]
    query_count: Literal[59]
    original_count: Literal[20]
    synthetic_count: Literal[39]
    construction_counts: QueryConstructionCountsResponse
    top_k: Literal[10]
    search_call_count: Literal[59]
    operational_failure_count: Literal[0]
    diagnostic_candidate_count: int = Field(ge=0, le=59)
    category_counts: BadCaseCategoryCounts
    samples: list[BadCaseSampleResponse] = Field(max_length=12)
    relevance_labels_used: Literal[False]
    relevance_metrics_computed: Literal[False]
    quality_metrics_computed: Literal[False]
    formal_evaluation_allowed: Literal[False]
    stage_drop_diagnostics_computed: Literal[False]
    locked_profiles_not_read: tuple[Literal["dev"], Literal["test"]]
    protected_profile_dispatch_count: Literal[0]
    strategy_write_count: Literal[0]
    worker_policy_id: Literal["posix-process-group-deadline-v1"]
    worker_deadline_ms: Literal[125000]
    supervisor_receipt_id: str = Field(
        pattern=r"^bad-case-supervisor-execution-[0-9a-f]{12}$"
    )
    term_grace_ms: Literal[1000]
    kill_grace_ms: Literal[1000]
    completion_observation: Literal[
        "worker_result",
        "deadline_boundary_recovery",
        "protocol_recovery",
    ]
    worker_hard_deadline_enforced: Literal[True]
    limitations: tuple[
        Literal["synthetic_queries_are_unjudged"],
        Literal["diagnostics_do_not_claim_relevance_improvement"],
        Literal["development_smoke_is_not_final_evaluation"],
        Literal["single_stage_catalog_cannot_diagnose_stage_drop"],
        Literal["worker_deadline_enforcement_is_execution_scope"],
    ]

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        counts = self.construction_counts
        if (
            counts.identity != 20
            or counts.adjacent_transposition != 20
            or counts.token_order_reversal != 19
        ):
            raise ValueError("Bad Case construction counts do not match 59 cases")
        values = self.category_counts.model_dump(mode="json")
        if any(value > self.diagnostic_candidate_count for value in values.values()):
            raise ValueError("category count exceeds diagnostic candidates")
        category_total = sum(values.values())
        if not (
            self.diagnostic_candidate_count
            <= category_total
            <= self.diagnostic_candidate_count * 4
        ):
            raise ValueError("category totals do not cover diagnostic candidates")
        if len(self.samples) > self.diagnostic_candidate_count:
            raise ValueError("display samples exceed diagnostic candidates")
        if len({sample.case_id for sample in self.samples}) != len(self.samples):
            raise ValueError("display sample cases must be unique")
        for sample in self.samples:
            if any(values[category] == 0 for category in sample.categories):
                raise ValueError("display sample category is absent from aggregate")
        return self


def _validate_api_display_hits(
    hits: list[BadCaseDisplayHitResponse],
    returned_at_k: int,
) -> None:
    if len(hits) > returned_at_k:
        raise ValueError("display hits exceed returned results")
    if [item.rank for item in hits] != list(range(1, len(hits) + 1)):
        raise ValueError("display hit ranks must be contiguous and ordered")
    if len({(item.locale, item.product_id) for item in hits}) != len(hits):
        raise ValueError("display hit product keys must be unique")


RetrievalGateName = Literal[
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


class RetrievalModelCallResponse(BaseModel):
    """One safe model-call receipt; prompts and provider bodies stay private."""

    model_config = ConfigDict(extra="forbid", strict=True)

    duration_ms: float = Field(ge=0.0, le=120_000.0, allow_inf_nan=False)
    input_tokens: int = Field(ge=0)
    model_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_token_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("model token total is inconsistent")
        return self


class RetrievalAgentActionResponse(BaseModel):
    """One privacy-safe action/observation pair exposed to the workbench."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decision_source: Literal["deterministic", "llm"]
    evidence_ref: str | None = Field(
        default=None,
        pattern=(
            r"^(?:run:retrieval-[0-9a-f]{12}|"
            r"comparison:retrieval-comparison-[0-9a-f]{12})$"
        ),
    )
    failed_gates: list[RetrievalGateName] = Field(max_length=12)
    gate_passed: bool | None
    model_call: RetrievalModelCallResponse | None
    pipeline_variant: (
        Literal[
            "title-exact-multifield-v1",
            "title-exact-multifield-weighted-v1",
            "title-exact-multifield-weighted-aggressive-v1",
        ]
        | None
    )
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    retryable: bool
    sequence: int = Field(ge=1)
    selected_option_id: (
        Literal[
            "diagnose_baseline",
            "run_uniform_candidate",
            "run_conservative_candidate",
            "run_aggressive_candidate",
        ]
        | None
    )
    status: Literal["succeeded", "failed"]
    tool_name: Literal[
        "diagnose_baseline_retrieval",
        "run_retrieval_candidate",
    ]

    @model_validator(mode="after")
    def validate_action_observation_pair(self) -> Self:
        is_baseline = self.tool_name == "diagnose_baseline_retrieval"
        if self.decision_source == "deterministic":
            if self.selected_option_id is not None or self.model_call is not None:
                raise ValueError("deterministic action contains model metadata")
        else:
            if self.selected_option_id is None or self.model_call is None:
                raise ValueError("LLM action is missing model metadata")
            expected_option = (
                "diagnose_baseline"
                if is_baseline
                else {
                    "title-exact-multifield-v1": "run_uniform_candidate",
                    "title-exact-multifield-weighted-v1": (
                        "run_conservative_candidate"
                    ),
                    "title-exact-multifield-weighted-aggressive-v1": (
                        "run_aggressive_candidate"
                    ),
                }.get(self.pipeline_variant)
            )
            if self.selected_option_id != expected_option:
                raise ValueError("LLM option does not match the recorded action")
        if is_baseline and self.pipeline_variant is not None:
            raise ValueError("baseline action must not declare a pipeline variant")
        if not is_baseline and self.pipeline_variant is None:
            raise ValueError("candidate action must declare a pipeline variant")
        if self.status == "failed":
            if (
                self.evidence_ref is not None
                or self.gate_passed is not None
                or self.failed_gates
                or not self.retryable
            ):
                raise ValueError("recoverable failed action shape is invalid")
            return self
        if self.retryable or self.evidence_ref is None:
            raise ValueError("successful action shape is invalid")
        if is_baseline:
            if self.gate_passed is not None or self.failed_gates:
                raise ValueError("baseline action must not declare gate evidence")
            if not self.evidence_ref.startswith("run:"):
                raise ValueError("baseline evidence must reference a Run")
        elif (
            not self.evidence_ref.startswith("comparison:")
            or self.gate_passed is None
            or self.gate_passed is not (not self.failed_gates)
        ):
            raise ValueError("candidate gate evidence shape is invalid")
        return self


class RetrievalLLMUsageResponse(BaseModel):
    """Aggregate model usage for one Trace, including the terminal decision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    input_tokens: int = Field(ge=0)
    model_calls: int = Field(ge=1, le=6)
    model_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    output_tokens: int = Field(ge=0)
    prompt_version: Literal["retrieval-choice-prompt-v1"]
    provider_id: Literal["openai", "volcengine_agent_plan"]
    terminal_option_id: Literal[
        "finish_best_passing_candidate",
        "finish_no_safe_improvement",
    ]
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_token_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("LLM token total is inconsistent")
        return self


class RetrievalAgentRunResponse(BaseModel):
    """Replay-validated Runtime summary; the full Trace remains server-side."""

    model_config = ConfigDict(extra="forbid", strict=True)

    actions: list[RetrievalAgentActionResponse] = Field(min_length=1, max_length=6)
    llm_usage: RetrievalLLMUsageResponse | None
    outcome: Literal["proposal_ready", "no_safe_improvement"]
    planner_id: Literal[
        "stage-aware-retrieval-planner-v1",
        "llm-retrieval-planner-v1",
    ]
    planner_mode: Literal["deterministic", "llm"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    replay_mode: Literal["recorded_trace"]
    runtime_id: Literal["search-agent-runtime-v1"]
    schema_version: Literal["retrieval-agent-run-summary-v2"]
    state: Literal["completed"]
    steps_used: int = Field(ge=1, le=8)
    tool_calls_used: int = Field(ge=1, le=6)
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_runtime_counts_and_recovery(self) -> Self:
        if self.tool_calls_used != len(self.actions):
            raise ValueError("tool call count must match action summary")
        if self.steps_used != self.tool_calls_used + 1:
            raise ValueError("completed Runtime requires one terminal step")
        for index, action in enumerate(self.actions):
            if action.sequence != index + 1:
                raise ValueError("action sequence must be contiguous")
            if action.status != "failed":
                continue
            if index + 1 >= len(self.actions):
                raise ValueError("failed action must have a recorded retry")
            retry = self.actions[index + 1]
            if (
                retry.status != "succeeded"
                or retry.tool_name != action.tool_name
                or retry.pipeline_variant != action.pipeline_variant
            ):
                raise ValueError("failed action retry must preserve action scope")
        if self.planner_mode == "deterministic":
            if (
                self.planner_id != "stage-aware-retrieval-planner-v1"
                or self.llm_usage is not None
                or any(
                    action.decision_source != "deterministic" for action in self.actions
                )
            ):
                raise ValueError("deterministic Runtime metadata is inconsistent")
            return self
        if (
            self.planner_id != "llm-retrieval-planner-v1"
            or self.llm_usage is None
            or any(action.decision_source != "llm" for action in self.actions)
            or self.llm_usage.model_calls != self.steps_used
        ):
            raise ValueError("LLM Runtime metadata is inconsistent")
        if any(
            action.model_call is None
            or action.model_call.model_id != self.llm_usage.model_id
            for action in self.actions
        ):
            raise ValueError("LLM action model does not match aggregate usage")
        action_input = sum(
            action.model_call.input_tokens
            for action in self.actions
            if action.model_call is not None
        )
        action_output = sum(
            action.model_call.output_tokens
            for action in self.actions
            if action.model_call is not None
        )
        if (
            action_input > self.llm_usage.input_tokens
            or action_output > self.llm_usage.output_tokens
        ):
            raise ValueError("LLM action usage exceeds the Trace total")
        expected_terminal = (
            "finish_best_passing_candidate"
            if self.outcome == "proposal_ready"
            else "finish_no_safe_improvement"
        )
        if self.llm_usage.terminal_option_id != expected_terminal:
            raise ValueError("LLM terminal option does not match the outcome")
        return self


class RetrievalAgentPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    max_elapsed_ms: Literal[120_000]
    max_failures: Literal[1, 3]
    max_run_creations: Literal[4, 5]
    max_same_action_attempts: Literal[1, 2]
    max_steps: Literal[6, 8]
    max_tool_calls: Literal[4, 6]


class RetrievalAgentStatusResponse(BaseModel):
    """Non-secret capability/configuration status for the Agent workbench."""

    model_config = ConfigDict(extra="forbid", strict=True)

    model_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
    )
    planner_id: Literal[
        "stage-aware-retrieval-planner-v1",
        "llm-retrieval-planner-v1",
    ]
    planner_mode: Literal["deterministic", "llm"]
    policy: RetrievalAgentPolicyResponse
    provider_id: Literal["openai", "volcengine_agent_plan"] | None
    runtime_id: Literal["search-agent-runtime-v1"]
    schema_version: Literal["retrieval-agent-status-v1"]
    state: Literal["deterministic", "ready", "not_configured"]
    strategy_write_allowed: Literal[False]
    tools: list[
        Literal[
            "diagnose_baseline_retrieval",
            "run_retrieval_candidate",
        ]
    ] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.tools != [
            "diagnose_baseline_retrieval",
            "run_retrieval_candidate",
        ]:
            raise ValueError("retrieval Agent tool allowlist is invalid")
        if self.planner_mode == "deterministic":
            if (
                self.state != "deterministic"
                or self.planner_id != "stage-aware-retrieval-planner-v1"
                or self.provider_id is not None
                or self.model_id is not None
                or self.policy.max_steps != 8
                or self.policy.max_tool_calls != 6
                or self.policy.max_run_creations != 5
                or self.policy.max_failures != 3
                or self.policy.max_same_action_attempts != 2
            ):
                raise ValueError("deterministic Agent status is inconsistent")
        elif (
            self.state == "deterministic"
            or self.planner_id != "llm-retrieval-planner-v1"
            or self.provider_id not in {"openai", "volcengine_agent_plan"}
            or self.policy.max_steps != 6
            or self.policy.max_tool_calls != 4
            or self.policy.max_run_creations != 4
            or self.policy.max_failures != 1
            or self.policy.max_same_action_attempts != 1
            or (self.state == "ready" and self.model_id is None)
        ):
            raise ValueError("LLM Agent status is inconsistent")
        return self


class RetrievalExampleResultResponse(BaseModel):
    """One bounded result row displayed inside a protected comparison card."""

    model_config = ConfigDict(extra="forbid", strict=True)

    label: Literal["E", "S", "C", "I"]
    locale: str = Field(min_length=1, max_length=32)
    product_id: str = Field(min_length=1, max_length=128)
    product_title: str = Field(min_length=1, max_length=2048)
    rank: int = Field(ge=1, le=10)


class RetrievalRecoveredResultResponse(BaseModel):
    """One relevant item newly recovered by the comparison candidate."""

    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_first_loss_stage: Literal["fusion", "coarse_rank", "retained"]
    candidate_multi_field_rank: int | None = Field(default=None, ge=1)
    label: Literal["E", "S", "C"]
    locale: str = Field(min_length=1, max_length=32)
    product_id: str = Field(min_length=1, max_length=128)
    product_title: str = Field(min_length=1, max_length=2048)


class RetrievalChangedQueryExampleResponse(BaseModel):
    """A non-tied before/after Query example from one bounded experiment."""

    model_config = ConfigDict(extra="forbid", strict=True)

    baseline_top_results: list[RetrievalExampleResultResponse] = Field(max_length=10)
    candidate_run_id: str = Field(pattern=r"^retrieval-[0-9a-f]{12}$")
    candidate_top_results: list[RetrievalExampleResultResponse] = Field(max_length=10)
    comparison_id: str = Field(pattern=r"^retrieval-comparison-[0-9a-f]{12}$")
    coarse_ndcg_at_10_delta: float = Field(
        alias="coarse_ndcg@10_delta",
        allow_inf_nan=False,
        ge=-1.0,
        le=1.0,
    )
    fusion_ndcg_at_10_delta: float = Field(
        alias="fusion_ndcg@10_delta",
        allow_inf_nan=False,
        ge=-1.0,
        le=1.0,
    )
    gate_passed: bool
    is_selected_comparison: bool
    locale: str = Field(min_length=1, max_length=32)
    outcome: Literal["improvement", "regression"]
    pipeline_variant: Literal[
        "title-exact-multifield-v1",
        "title-exact-multifield-weighted-v1",
        "title-exact-multifield-weighted-aggressive-v1",
    ]
    query_id: int = Field(ge=1)
    query_text: str = Field(min_length=1, max_length=512)
    recovered_relevant: list[RetrievalRecoveredResultResponse]
    union_coverage_delta: float = Field(
        allow_inf_nan=False,
        ge=-1.0,
        le=1.0,
    )

    @model_validator(mode="after")
    def validate_outcome_and_results(self) -> Self:
        delta = self.coarse_ndcg_at_10_delta
        if self.outcome == "improvement" and delta <= 1e-12:
            raise ValueError("improvement example must have positive coarse nDCG delta")
        if self.outcome == "regression" and delta >= -1e-12:
            raise ValueError("regression example must have negative coarse nDCG delta")
        for results in (self.baseline_top_results, self.candidate_top_results):
            ranks = [item.rank for item in results]
            if ranks != list(range(1, len(results) + 1)):
                raise ValueError("comparison result ranks must be contiguous")
        return self


class RetrievalAnalysisResponse(BaseModel):
    """Strict top-level contract for the authenticated Agent workbench."""

    model_config = ConfigDict(extra="forbid", strict=True)

    aggregate: dict
    agent_run: RetrievalAgentRunResponse
    candidate_aggregate: dict
    candidate_diagnosis: dict
    candidate_diagnosis_id: str = Field(pattern=r"^stage-diagnosis-[0-9a-f]{12}$")
    candidate_run_id: str = Field(pattern=r"^retrieval-[0-9a-f]{12}$")
    changed_query_examples: list[RetrievalChangedQueryExampleResponse] = Field(
        max_length=10
    )
    comparison: dict
    comparison_id: str = Field(pattern=r"^retrieval-comparison-[0-9a-f]{12}$")
    diagnosis: dict
    diagnosis_id: str = Field(pattern=r"^stage-diagnosis-[0-9a-f]{12}$")
    evaluation_boundary: dict
    experiments: list[dict]
    pipeline: dict
    pipeline_id: str = Field(pattern=r"^pipeline-[0-9a-f]{12}$")
    profile: Literal["smoke"]
    proposal: dict
    retrieval_run_id: str = Field(pattern=r"^retrieval-[0-9a-f]{12}$")
    schema_version: Literal["retrieval-stage-analysis-response-v1"]
    status: Literal["proposal_ready", "no_safe_improvement"]

    @model_validator(mode="after")
    def validate_changed_examples(self) -> Self:
        experiments = {
            item.get("comparison_id"): item
            for item in self.experiments
            if isinstance(item, dict)
        }
        keys: set[tuple[str, int, str]] = set()
        for example in self.changed_query_examples:
            key = (example.locale, example.query_id, example.outcome)
            if key in keys:
                raise ValueError("changed Query examples must be unique by outcome")
            keys.add(key)
            source = experiments.get(example.comparison_id)
            if source is None:
                raise ValueError("changed Query example has no experiment source")
            if (
                source.get("candidate_run_id") != example.candidate_run_id
                or source.get("pipeline_variant") != example.pipeline_variant
                or source.get("gate_passed") is not example.gate_passed
            ):
                raise ValueError(
                    "changed Query example source does not match experiment"
                )
            selected = example.comparison_id == self.comparison_id
            if example.is_selected_comparison is not selected:
                raise ValueError("changed Query example selected state is inconsistent")
        return self


class StrategyDecisionRequest(BaseModel):
    """Human decision for one pending strategy proposal."""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{12}$")
    decision: Literal["approve", "reject"]


@app.get("/smoke", deprecated=True)
def smoke(
    query: str = Query(default="wireless mouse", min_length=1, max_length=200),
    top_k: int = Query(default=5, ge=1, le=10),
    backend: str = Query(default=os.environ.get("SEARCH_BACKEND", "local")),
) -> dict:
    """Compatibility transport; prefer POST so Query text is not in the URL."""

    if backend not in {"local", "opensearch"}:
        raise HTTPException(status_code=400, detail="unsupported backend")
    try:
        return run_smoke(backend_name=backend, query=query, top_k=top_k)
    except (RuntimeError, ValueError, OSError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "smoke_search_failed",
            extra={
                "backend": backend,
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "search_backend_unavailable",
                "message": "Search backend unavailable",
                "trace_id": trace_id,
            },
        ) from exc


@app.post("/smoke")
def smoke_post(request: SmokeRequest) -> dict:
    """Preferred public transport: Query text stays in the request body."""

    return smoke(query=request.query, top_k=request.top_k, backend=request.backend)


@app.post("/catalog/search")
def catalog_search_post(request: CatalogSearchRequest) -> dict:
    """Search all 1,814,924 official ESCI products through the baseline index."""

    try:
        return (
            get_catalog_search_service()
            .search(
                request.query,
                top_k=request.top_k,
            )
            .to_dict()
        )
    except InvalidCatalogQuery as exc:
        logger.debug(
            "catalog_query_rejected",
            extra={"error_code": "invalid_catalog_query"},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_catalog_query",
                "message": "Search query is invalid",
                "trace_id": current_trace_id(),
            },
        ) from exc
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        trace_id = current_trace_id()
        logger.error(
            "catalog_search_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "catalog_search_unavailable",
                "message": "Catalog search unavailable",
                "trace_id": trace_id,
            },
        ) from exc


@app.post("/agent/strategy/propose")
def agent_strategy_propose(request: StrategyProposalRequest) -> dict:
    """Run the smoke Agent optimization workflow and return a proposal panel."""

    try:
        return _cached_agent_strategy_proposal(profile_id=request.profile)
    except StrategyProposalRejectedError as exc:
        logger.debug(
            "agent_strategy_proposal_rejected",
            extra={"error_code": classify_error(exc)},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_strategy_proposal_request",
                "message": "Strategy proposal request is invalid",
                "trace_id": current_trace_id(),
            },
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "agent_strategy_proposal_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "strategy_proposal_unavailable",
                "message": "Strategy proposal workflow unavailable",
                "trace_id": trace_id,
            },
        ) from exc


@app.get("/agent/retrieval/status", response_model=RetrievalAgentStatusResponse)
def agent_retrieval_status() -> dict:
    """Expose planner readiness and hard policy limits without exposing a key."""

    try:
        config = load_retrieval_planner_configuration()
    except LLMPlannerConfigurationError as exc:
        logger.error(
            "agent_retrieval_configuration_invalid",
            extra={"error_code": str(exc)},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "retrieval_agent_configuration_invalid",
                "message": "Retrieval Agent configuration is invalid",
                "trace_id": current_trace_id(),
            },
        ) from exc
    llm_mode = config.planner_mode == "llm"
    return {
        "model_id": config.model_id,
        "planner_id": config.planner_id,
        "planner_mode": config.planner_mode,
        "policy": {
            "max_elapsed_ms": 120_000,
            "max_failures": 1 if llm_mode else 3,
            "max_run_creations": 4 if llm_mode else 5,
            "max_same_action_attempts": 1 if llm_mode else 2,
            "max_steps": 6 if llm_mode else 8,
            "max_tool_calls": 4 if llm_mode else 6,
        },
        "provider_id": config.provider_id,
        "runtime_id": "search-agent-runtime-v1",
        "schema_version": "retrieval-agent-status-v1",
        "state": config.state,
        "strategy_write_allowed": False,
        "tools": [
            "diagnose_baseline_retrieval",
            "run_retrieval_candidate",
        ],
    }


@app.post("/agent/retrieval/analyze", response_model=RetrievalAnalysisResponse)
def agent_retrieval_analyze(request: RetrievalAnalysisRequest) -> dict:
    """Diagnose recall, fusion and coarse-rank evidence before proposing changes."""

    if not _RETRIEVAL_ANALYSIS_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "retrieval_analysis_in_progress",
                "message": "Retrieval analysis is already running",
                "trace_id": current_trace_id(),
            },
        )
    try:
        config = load_retrieval_planner_configuration()
        planner = build_retrieval_planner(config)
        return generate_retrieval_runtime_analysis(
            project_root=PROJECT_ROOT,
            artifact_root=_agent_artifact_root(),
            profile_id=request.profile,
            revision_provider=_api_code_revision,
            planner=planner,
        )
    except LLMPlannerConfigurationError as exc:
        logger.error(
            "agent_retrieval_planner_unavailable",
            extra={"error_code": str(exc)},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "llm_planner_not_configured",
                "message": "LLM retrieval Planner is not configured",
                "trace_id": current_trace_id(),
            },
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "agent_retrieval_analysis_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "retrieval_analysis_unavailable",
                "message": "Retrieval analysis workflow unavailable",
                "trace_id": trace_id,
            },
        ) from exc
    finally:
        _RETRIEVAL_ANALYSIS_LOCK.release()


@app.post("/agent/eval/run", response_model=AgentEvalResponse)
def agent_eval_run(request: AgentEvalRequest) -> dict:
    """Evaluate Agent behavior without activating or changing any strategy."""

    if not _AGENT_EVAL_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "agent_eval_in_progress",
                "message": "Agent evaluation is already running",
                "trace_id": current_trace_id(),
            },
        )
    try:
        result = run_agent_eval_suite(
            project_root=PROJECT_ROOT,
            artifact_root=_agent_artifact_root(),
            suite_id=request.suite,
            revision_provider=_api_code_revision,
        )
        evidence = result.evidence
        return {
            "schema_version": "agent-eval-api-summary-v1",
            "suite_id": evidence.suite_id,
            "evidence_id": evidence.evidence_id,
            "execution_id": result.execution.execution_id,
            "formal_passed": evidence.formal_passed,
            "task_count": len(evidence.tasks),
            "metrics": evidence.metrics.model_dump(mode="json"),
            "subject_summaries": tuple(
                {
                    "subject_kind": item.subject_kind,
                    "task_count": item.task_count,
                    "passed_count": item.passed_count,
                }
                for item in evidence.subject_summaries
            ),
            "limitations": evidence.limitations,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "agent_eval_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "agent_eval_unavailable",
                "message": "Agent evaluation unavailable",
                "trace_id": trace_id,
            },
        ) from exc
    finally:
        _AGENT_EVAL_LOCK.release()


@app.post(
    "/agent/query-constructor/build",
    response_model=QueryConstructorResponse,
)
def agent_query_constructor_build(request: QueryConstructorRequest) -> dict:
    """Create an exploratory Query set from smoke without reading locked splits."""

    try:
        artifact = build_smoke_query_set(
            project_root=PROJECT_ROOT,
            source_profile=request.source,
            revision_provider=_api_code_revision,
        )
        store_query_set(
            artifact,
            artifact_root=_agent_artifact_root() or PROJECT_ROOT / "runs",
        )
        counts = Counter(item.construction.value for item in artifact.cases)
        return {
            "schema_version": "query-constructor-api-summary-v1",
            "source": artifact.source_profile,
            "query_set_id": artifact.query_set_id,
            "query_count": artifact.query_count,
            "original_count": artifact.original_count,
            "synthetic_count": artifact.synthetic_count,
            "deduplicated_count": artifact.deduplicated_count,
            "construction_counts": {
                "identity": counts["identity"],
                "adjacent_transposition": counts["adjacent_transposition"],
                "token_order_reversal": counts["token_order_reversal"],
            },
            "formal_evaluation_allowed": artifact.formal_evaluation_allowed,
            "locked_profiles_not_read": artifact.locked_profiles_not_read,
            "cross_split_collision_status": artifact.cross_split_collision_status,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "query_constructor_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "query_constructor_unavailable",
                "message": "Query constructor unavailable",
                "trace_id": trace_id,
            },
        ) from exc


@app.post("/agent/bad-cases/run", response_model=BadCaseRunResponse)
def agent_bad_cases_run(request: BadCaseRunRequest) -> dict:
    """Run 59 label-blind diagnostics without changing the search strategy."""

    if not _BAD_CASE_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "bad_case_run_in_progress",
                "message": "Bad Case diagnostics are already running",
                "trace_id": current_trace_id(),
            },
        )
    try:
        run = supervise_bad_case_diagnostics(
            project_root=PROJECT_ROOT,
            artifact_root=_agent_artifact_root(),
            catalog_index_path=_catalog_index_path(),
            executor_revision=_api_code_revision(PROJECT_ROOT),
            source_profile=request.source,
            deadline_ms=DEFAULT_WORKER_DEADLINE_MS,
            trace_id=current_trace_id(),
        )
        artifact = run.artifact
        supervisor_receipt = load_supervisor_execution_receipt(
            _runtime_artifact_root(),
            run.execution.execution_id,
        )
        if (
            supervisor_receipt.diagnostic_id != artifact.diagnostic_id
            or supervisor_receipt.policy_id != "posix-process-group-deadline-v1"
            or supervisor_receipt.deadline_ms != DEFAULT_WORKER_DEADLINE_MS
            or supervisor_receipt.term_grace_ms != DEFAULT_TERM_GRACE_MS
            or supervisor_receipt.kill_grace_ms != DEFAULT_KILL_GRACE_MS
            or supervisor_receipt.completed is not True
        ):
            raise ValueError("Bad Case supervisor receipt contradicts the API run")
        return {
            "schema_version": "bad-case-api-summary-v2",
            "completed": artifact.completed,
            "diagnostic_id": artifact.diagnostic_id,
            "execution_id": run.execution.execution_id,
            "query_set_id": artifact.query_set_id,
            "index_id": artifact.index_id,
            "search_strategy_id": artifact.search_strategy_id,
            "query_count": artifact.query_count,
            "original_count": artifact.original_count,
            "synthetic_count": artifact.synthetic_count,
            "construction_counts": artifact.construction_counts,
            "top_k": artifact.top_k,
            "search_call_count": artifact.search_call_count,
            "operational_failure_count": artifact.operational_failure_count,
            "diagnostic_candidate_count": artifact.diagnostic_candidate_count,
            "category_counts": artifact.category_counts.model_dump(mode="json"),
            "samples": [item.model_dump(mode="json") for item in run.samples],
            "relevance_labels_used": artifact.relevance_labels_used,
            "relevance_metrics_computed": artifact.relevance_metrics_computed,
            "quality_metrics_computed": artifact.quality_metrics_computed,
            "formal_evaluation_allowed": artifact.formal_evaluation_allowed,
            "stage_drop_diagnostics_computed": (
                artifact.stage_drop_diagnostics_computed
            ),
            "locked_profiles_not_read": artifact.locked_profiles_not_read,
            "protected_profile_dispatch_count": (
                artifact.protected_profile_dispatch_count
            ),
            "strategy_write_count": artifact.strategy_write_count,
            "worker_policy_id": supervisor_receipt.policy_id,
            "worker_deadline_ms": supervisor_receipt.deadline_ms,
            "supervisor_receipt_id": supervisor_receipt.receipt_id,
            "term_grace_ms": supervisor_receipt.term_grace_ms,
            "kill_grace_ms": supervisor_receipt.kill_grace_ms,
            "completion_observation": supervisor_receipt.completion_observation,
            "worker_hard_deadline_enforced": True,
            "limitations": (
                "synthetic_queries_are_unjudged",
                "diagnostics_do_not_claim_relevance_improvement",
                "development_smoke_is_not_final_evaluation",
                "single_stage_catalog_cannot_diagnose_stage_drop",
                "worker_deadline_enforcement_is_execution_scope",
            ),
        }
    except BadCaseRunInProgress as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "bad_case_run_in_progress",
                "message": "Bad Case diagnostics are already running",
                "trace_id": current_trace_id(),
            },
        ) from exc
    except BadCaseWorkerDeadlineExceeded as exc:
        trace_id = current_trace_id()
        logger.error(
            "bad_case_worker_deadline_exceeded",
            extra={
                "error_code": exc.error_code,
                "execution_id": exc.execution_id,
            },
        )
        raise HTTPException(
            status_code=504,
            detail={
                "code": "bad_case_worker_deadline_exceeded",
                "message": "Bad Case diagnostics exceeded the worker deadline",
                "trace_id": trace_id,
                "execution_id": exc.execution_id,
            },
        ) from exc
    except (
        BadCaseWorkerError,
        OSError,
        RuntimeError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        trace_id = current_trace_id()
        logger.error(
            "bad_case_run_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "bad_case_run_unavailable",
                "message": "Bad Case diagnostics unavailable",
                "trace_id": trace_id,
            },
        ) from exc
    finally:
        _BAD_CASE_LOCK.release()


@app.post(
    "/agent/diagnostic-experiments/plan",
    response_model=DiagnosticExperimentPlan,
)
def agent_diagnostic_experiment_plan(
    request: DiagnosticExperimentPlanRequest,
) -> DiagnosticExperimentPlan:
    """Map trusted behavior evidence to one bounded, non-mutating experiment."""

    try:
        evidence = load_resolved_diagnostic_evidence(
            artifact_root=_runtime_artifact_root(),
            diagnostic_id=request.diagnostic_id,
            query_set_id=request.query_set_id,
        )
        return route_diagnostic_evidence(evidence)
    except (OSError, RuntimeError, ValueError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "diagnostic_experiment_plan_failed",
            extra={
                "diagnostic_id": request.diagnostic_id,
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
                "query_set_id": request.query_set_id,
            },
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "diagnostic_evidence_unavailable",
                "message": "Diagnostic evidence is unavailable or stale",
                "trace_id": trace_id,
            },
        ) from exc


@app.post(
    "/agent/human-oracle/batches/create",
    response_model=HumanOracleBatchCreateResponse,
)
def human_oracle_batch_create(
    http_request: Request,
    payload: HumanOracleBatchCreateRequest,
) -> dict:
    """Create the fixed 20-cluster census from immutable diagnostic evidence."""

    _human_oracle_actor_from_request(http_request)
    try:
        diagnostic, query_set = load_diagnostic_artifacts(
            artifact_root=_runtime_artifact_root(),
            diagnostic_id=payload.diagnostic_id,
            query_set_id=payload.query_set_id,
        )
        batch = build_oracle_batch(diagnostic=diagnostic, query_set=query_set)
        repository = _human_oracle_repository()
        batch = repository.create_batch(
            batch,
            diagnostic=diagnostic,
            query_set=query_set,
        )
        response = {
            "schema_version": "human-oracle-batch-api-summary-v1",
            "oracle_batch_id": batch.oracle_batch_id,
            "diagnostic_id": batch.diagnostic_id,
            "query_set_id": batch.query_set_id,
            "selected_cluster_count": batch.selected_cluster_count,
            "selected_candidate_count": batch.selected_candidate_count,
            "synthetic_intent_candidate_count": (
                batch.synthetic_intent_candidate_count
            ),
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "source_case_id": unit.source_case_id,
                    "stratum": unit.stratum.value,
                    "candidate_count": len(unit.candidates),
                }
                for unit in batch.units
            ],
            "formal_evaluation_allowed": batch.formal_evaluation_allowed,
            "quality_conclusion_allowed": batch.quality_conclusion_allowed,
            "strategy_write_count": batch.strategy_write_count,
        }
        logger.info(
            "human_oracle_api_completed",
            extra={
                "diagnostic_id": batch.diagnostic_id,
                "operation": "batch_create",
                "oracle_batch_id": batch.oracle_batch_id,
            },
        )
        return response
    except (FileNotFoundError, ValueError) as exc:
        _raise_human_oracle_conflict(operation="batch_create", error=exc)
    except (OracleStorageError, OSError) as exc:
        _raise_human_oracle_unavailable(operation="batch_create", error=exc)


@app.post(
    "/agent/human-oracle/batches/status",
    response_model=OracleReviewState,
)
def human_oracle_batch_status(
    http_request: Request,
    payload: HumanOracleBatchRequest,
) -> OracleReviewState:
    """Return safe progress/CAS heads without raw Query or result content."""

    _human_oracle_actor_from_request(http_request)
    try:
        repository = _human_oracle_repository()
        _load_human_oracle_evidence(repository, payload.oracle_batch_id)
        state = repository.review_state(payload.oracle_batch_id)
        logger.info(
            "human_oracle_api_completed",
            extra={
                "operation": "batch_status",
                "oracle_batch_id": state.oracle_batch_id,
            },
        )
        return state
    except (ValueError, FileNotFoundError) as exc:
        _raise_human_oracle_conflict(
            operation="batch_status",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )
    except (OracleStorageError, OSError) as exc:
        _raise_human_oracle_unavailable(
            operation="batch_status",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )


@app.post(
    "/agent/human-oracle/intents/view",
    response_model=OracleIntentView,
)
def human_oracle_intent_view(
    http_request: Request,
    payload: HumanOracleUnitRequest,
) -> OracleIntentView:
    """Present Query text alone; result evidence stays withheld in phase one."""

    _human_oracle_actor_from_request(http_request)
    try:
        repository = _human_oracle_repository()
        batch, _diagnostic, query_set = _load_human_oracle_evidence(
            repository,
            payload.oracle_batch_id,
        )
        view = build_intent_view(
            batch=batch,
            query_set=query_set,
            unit_id=payload.unit_id,
        )
        logger.info(
            "human_oracle_api_completed",
            extra={
                "operation": "intent_view",
                "oracle_batch_id": batch.oracle_batch_id,
                "unit_id": view.unit_id,
            },
        )
        return view
    except (ValueError, FileNotFoundError) as exc:
        _raise_human_oracle_conflict(
            operation="intent_view",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
            unit_id=payload.unit_id,
        )
    except (OracleStorageError, OSError) as exc:
        _raise_human_oracle_unavailable(
            operation="intent_view",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )


@app.post(
    "/agent/human-oracle/intents/submit",
    response_model=HumanOracleIntentSubmitResponse,
)
def human_oracle_intent_submit(
    http_request: Request,
    payload: HumanOracleIntentSubmitRequest,
) -> dict:
    """Store one intent judgment with CAS and idempotency controls."""

    actor = _human_oracle_actor_from_request(http_request)
    try:
        repository = _human_oracle_repository()
        _load_human_oracle_evidence(repository, payload.oracle_batch_id)
        annotation = repository.submit_intent(
            IntentSubmission(
                **payload.model_dump(
                    mode="python",
                    exclude={"judgment", "reason_code"},
                ),
                judgment=IntentJudgment(payload.judgment),
                reason_code=IntentReason(payload.reason_code),
                actor=actor,
            )
        )
        logger.info(
            "human_oracle_api_completed",
            extra={
                "intent_annotation_id": annotation.intent_annotation_id,
                "operation": "intent_submit",
                "oracle_batch_id": annotation.oracle_batch_id,
                "unit_id": annotation.unit_id,
            },
        )
        return {
            "schema_version": "human-oracle-intent-api-summary-v1",
            "intent_annotation_id": annotation.intent_annotation_id,
            "oracle_batch_id": annotation.oracle_batch_id,
            "unit_id": annotation.unit_id,
            "case_id": annotation.case_id,
            "judgment": annotation.judgment,
            "reason_code": annotation.reason_code,
            "supersedes_annotation_id": annotation.supersedes_annotation_id,
            "result_evidence_was_withheld": (
                annotation.oracle_ui_withheld_result_evidence
            ),
            "product_relevance_labels_created": (
                annotation.product_relevance_labels_created
            ),
        }
    except OracleInvalidDecision as exc:
        _raise_human_oracle_invalid_decision(
            operation="intent_submit",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
            unit_id=payload.unit_id,
        )
    except (
        OracleBatchSealed,
        OracleClientActionConflict,
        OracleCompareAndSwapConflict,
        ValueError,
        FileNotFoundError,
    ) as exc:
        _raise_human_oracle_conflict(
            operation="intent_submit",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
            unit_id=payload.unit_id,
        )
    except (OracleStorageError, OSError) as exc:
        _raise_human_oracle_unavailable(
            operation="intent_submit",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )


@app.post(
    "/agent/human-oracle/behaviors/view",
    response_model=OracleBehaviorView,
)
def human_oracle_behavior_view(
    http_request: Request,
    payload: HumanOracleUnitRequest,
) -> OracleBehaviorView:
    """Re-run and verify one unit after all 30 intent judgments exist."""

    _human_oracle_actor_from_request(http_request)
    try:
        repository = _human_oracle_repository()
        batch, diagnostic, query_set = _load_human_oracle_evidence(
            repository,
            payload.oracle_batch_id,
        )
        state = repository.review_state(batch.oracle_batch_id)
        if state.projection.active_intent_annotation_count != 30:
            raise OracleBatchIncomplete(
                "all 30 intent judgments are required before behavior evidence"
            )
        unit = next(
            (item for item in batch.units if item.unit_id == payload.unit_id),
            None,
        )
        if unit is None:
            raise ValueError("Oracle unit does not belong to its batch")
        active_intents = {}
        for candidate in unit.candidates:
            intent = repository.active_intent_for_case(
                batch.oracle_batch_id,
                candidate.case_id,
            )
            if intent is not None:
                active_intents[candidate.case_id] = intent
        samples = collect_behavior_samples_for_unit(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=payload.unit_id,
            search_service=_get_human_oracle_catalog_service(),
        )
        view = build_behavior_view(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=payload.unit_id,
            samples=samples,
            active_intents=active_intents,
        )
        logger.info(
            "human_oracle_api_completed",
            extra={
                "operation": "behavior_view",
                "oracle_batch_id": batch.oracle_batch_id,
                "unit_id": view.unit_id,
            },
        )
        return view
    except (
        OracleBatchIncomplete,
        RuntimeError,
        ValueError,
        FileNotFoundError,
    ) as exc:
        _raise_human_oracle_conflict(
            operation="behavior_view",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
            unit_id=payload.unit_id,
        )
    except (OracleStorageError, OSError, sqlite3.Error) as exc:
        _raise_human_oracle_unavailable(
            operation="behavior_view",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )


@app.post(
    "/agent/human-oracle/behaviors/submit",
    response_model=HumanOracleBehaviorSubmitResponse,
)
def human_oracle_behavior_submit(
    http_request: Request,
    payload: HumanOracleBehaviorSubmitRequest,
) -> dict:
    """Store one behavior judgment; clients cannot upload result evidence."""

    actor = _human_oracle_actor_from_request(http_request)
    try:
        repository = _human_oracle_repository()
        _load_human_oracle_evidence(repository, payload.oracle_batch_id)
        state = repository.review_state(payload.oracle_batch_id)
        if state.projection.active_intent_annotation_count != 30:
            raise OracleBatchIncomplete(
                "all 30 intent judgments are required before behavior judgments"
            )
        annotation = repository.submit_behavior(
            BehaviorSubmission(
                **payload.model_dump(
                    mode="python",
                    exclude={"judgment", "reason_code"},
                ),
                judgment=BehaviorJudgment(payload.judgment),
                reason_code=BehaviorReason(payload.reason_code),
                actor=actor,
            )
        )
        logger.info(
            "human_oracle_api_completed",
            extra={
                "behavior_annotation_id": annotation.behavior_annotation_id,
                "operation": "behavior_submit",
                "oracle_batch_id": annotation.oracle_batch_id,
                "unit_id": annotation.unit_id,
            },
        )
        return {
            "schema_version": "human-oracle-behavior-api-summary-v1",
            "behavior_annotation_id": annotation.behavior_annotation_id,
            "oracle_batch_id": annotation.oracle_batch_id,
            "unit_id": annotation.unit_id,
            "case_id": annotation.case_id,
            "judgment": annotation.judgment,
            "reason_code": annotation.reason_code,
            "intent_annotation_id": annotation.intent_annotation_id,
            "supersedes_annotation_id": annotation.supersedes_annotation_id,
            "product_relevance_labels_created": (
                annotation.product_relevance_labels_created
            ),
            "root_cause_claimed": annotation.root_cause_claimed,
        }
    except OracleInvalidDecision as exc:
        _raise_human_oracle_invalid_decision(
            operation="behavior_submit",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
            unit_id=payload.unit_id,
        )
    except (
        OracleBatchIncomplete,
        OracleBatchSealed,
        OracleClientActionConflict,
        OracleCompareAndSwapConflict,
        ValueError,
        FileNotFoundError,
    ) as exc:
        _raise_human_oracle_conflict(
            operation="behavior_submit",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
            unit_id=payload.unit_id,
        )
    except (OracleStorageError, OSError) as exc:
        _raise_human_oracle_unavailable(
            operation="behavior_submit",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )


@app.post(
    "/agent/human-oracle/batches/seal",
    response_model=HumanOracleSealResponse,
)
def human_oracle_batch_seal(
    http_request: Request,
    payload: HumanOracleSealRequest,
) -> dict:
    """Seal complete diagnostic judgments without writing any search strategy."""

    actor = _human_oracle_actor_from_request(http_request)
    try:
        repository = _human_oracle_repository()
        _load_human_oracle_evidence(repository, payload.oracle_batch_id)
        oracle = repository.seal(
            SealSubmission(
                oracle_batch_id=payload.oracle_batch_id,
                client_action_id=payload.client_action_id,
                actor=actor,
            )
        )
        logger.info(
            "human_oracle_api_completed",
            extra={
                "operation": "batch_seal",
                "oracle_batch_id": oracle.oracle_batch_id,
                "oracle_id": oracle.oracle_id,
            },
        )
        return _human_oracle_seal_response(oracle)
    except OracleInvalidDecision as exc:
        _raise_human_oracle_invalid_decision(
            operation="batch_seal",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )
    except (
        OracleBatchIncomplete,
        OracleBatchSealed,
        OracleClientActionConflict,
        ValueError,
        FileNotFoundError,
    ) as exc:
        _raise_human_oracle_conflict(
            operation="batch_seal",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )
    except (OracleStorageError, OSError) as exc:
        _raise_human_oracle_unavailable(
            operation="batch_seal",
            error=exc,
            oracle_batch_id=payload.oracle_batch_id,
        )


def _human_oracle_seal_response(oracle: HumanOracleArtifact) -> dict:
    return {
        "schema_version": "human-oracle-seal-api-summary-v1",
        "oracle_id": oracle.oracle_id,
        "oracle_batch_id": oracle.oracle_batch_id,
        "diagnostic_id": oracle.diagnostic_id,
        "synthetic_intent_annotation_count": (oracle.synthetic_intent_annotation_count),
        "behavior_annotation_count": oracle.behavior_annotation_count,
        "product_relevance_labels_created": oracle.product_relevance_labels_created,
        "formal_evaluation_allowed": oracle.formal_evaluation_allowed,
        "quality_conclusion_allowed": oracle.quality_conclusion_allowed,
        "root_cause_claimed": oracle.root_cause_claimed,
        "strategy_write_count": oracle.strategy_write_count,
        "limitations": oracle.limitations,
    }


@app.get("/agent/strategy/catalog")
def agent_strategy_catalog() -> dict:
    """Return approved strategies visible to the portfolio strategy platform."""

    try:
        return load_strategy_catalog(
            project_root=PROJECT_ROOT,
            artifact_root=_agent_artifact_root(),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "agent_strategy_catalog_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "strategy_catalog_unavailable",
                "message": "Strategy catalog unavailable",
                "trace_id": trace_id,
            },
        ) from exc


@app.post("/agent/strategy/decision")
def agent_strategy_decision(request: Request, payload: StrategyDecisionRequest) -> dict:
    """Record a decision only from the server's loopback owner channel."""

    if not _is_local_owner_request(request):
        logger.warning("agent_strategy_decision_forbidden")
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": "Resource not found",
                "trace_id": current_trace_id(),
            },
        )

    try:
        result = apply_strategy_decision(
            project_root=PROJECT_ROOT,
            artifact_root=_agent_artifact_root(),
            proposal_id=payload.proposal_id,
            decision=payload.decision,
            revision_provider=_api_code_revision,
        )
        with _AGENT_PROPOSAL_LOCK:
            _AGENT_PROPOSAL_CACHE.clear()
        logger.debug("agent_strategy_proposal_cache_cleared")
        return result
    except ValueError as exc:
        logger.debug(
            "agent_strategy_decision_rejected",
            extra={"error_code": classify_error(exc)},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_strategy_decision",
                "message": "Strategy decision is invalid",
                "trace_id": current_trace_id(),
            },
        ) from exc
    except (OSError, RuntimeError) as exc:
        trace_id = current_trace_id()
        logger.error(
            "agent_strategy_decision_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "strategy_decision_unavailable",
                "message": "Strategy decision workflow unavailable",
                "trace_id": trace_id,
            },
        ) from exc
