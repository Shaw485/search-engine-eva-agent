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
from pathlib import Path
from typing import Literal, Self

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

from search_quality.agent.optimization import (
    ActiveStrategyChangedError,
    StrategyProposalRejectedError,
    apply_strategy_decision,
    generate_strategy_proposal,
    load_strategy_catalog,
)
from search_quality.agent.retrieval_runtime import generate_retrieval_runtime_analysis
from search_quality.agent_eval.runner import run_agent_eval_suite
from search_quality.catalog import (
    DEFAULT_CATALOG_INDEX,
    CatalogSearchService,
    InvalidCatalogQuery,
)
from search_quality.evaluation.artifacts import require_clean_code_revision
from search_quality.observability import (
    classify_error,
    configure_logging,
    current_trace_id,
    logging_context,
    new_trace_id,
)
from search_quality.query_constructor import build_smoke_query_set, store_query_set
from search_quality.smoke import run_smoke

configure_logging()
logger = logging.getLogger("search_quality.api")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CODE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_AGENT_PROPOSAL_LOCK = threading.Lock()
_AGENT_EVAL_LOCK = threading.Lock()
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
                "/agent/eval/run",
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


class RetrievalAgentActionResponse(BaseModel):
    """One privacy-safe action/observation pair exposed to the workbench."""

    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_ref: str | None = Field(
        default=None,
        pattern=(
            r"^(?:run:retrieval-[0-9a-f]{12}|"
            r"comparison:retrieval-comparison-[0-9a-f]{12})$"
        ),
    )
    failed_gates: list[RetrievalGateName] = Field(max_length=12)
    gate_passed: bool | None
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
    status: Literal["succeeded", "failed"]
    tool_name: Literal[
        "diagnose_baseline_retrieval",
        "run_retrieval_candidate",
    ]

    @model_validator(mode="after")
    def validate_action_observation_pair(self) -> Self:
        is_baseline = self.tool_name == "diagnose_baseline_retrieval"
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


class RetrievalAgentRunResponse(BaseModel):
    """Replay-validated Runtime summary; the full Trace remains server-side."""

    model_config = ConfigDict(extra="forbid", strict=True)

    actions: list[RetrievalAgentActionResponse] = Field(min_length=1, max_length=6)
    outcome: Literal["proposal_ready", "no_safe_improvement"]
    planner_id: Literal["stage-aware-retrieval-planner-v1"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    replay_supported: Literal[True]
    runtime_id: Literal["search-agent-runtime-v1"]
    schema_version: Literal["retrieval-agent-run-summary-v1"]
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


@app.post("/agent/retrieval/analyze", response_model=RetrievalAnalysisResponse)
def agent_retrieval_analyze(request: RetrievalAnalysisRequest) -> dict:
    """Diagnose recall, fusion and coarse-rank evidence before proposing changes."""

    try:
        return generate_retrieval_runtime_analysis(
            project_root=PROJECT_ROOT,
            artifact_root=_agent_artifact_root(),
            profile_id=request.profile,
            revision_provider=_api_code_revision,
        )
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
