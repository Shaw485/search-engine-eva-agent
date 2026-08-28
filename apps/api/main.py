"""Small Stage 0 API exposing the same smoke contract as the CLI."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from search_quality.agent.optimization import (
    ActiveStrategyChangedError,
    StrategyProposalRejectedError,
    apply_strategy_decision,
    generate_strategy_proposal,
    load_strategy_catalog,
)
from search_quality.agent.retrieval_analysis import generate_retrieval_analysis
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
from search_quality.smoke import run_smoke

configure_logging()
logger = logging.getLogger("search_quality.api")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CODE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_AGENT_PROPOSAL_LOCK = threading.Lock()
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

    profile: Literal["smoke"] = "smoke"


class RetrievalAnalysisResponse(BaseModel):
    """Strict top-level contract for the authenticated Agent workbench."""

    model_config = ConfigDict(extra="forbid", strict=True)

    aggregate: dict
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
        return generate_retrieval_analysis(
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
