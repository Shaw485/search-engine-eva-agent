"""Small Stage 0 API exposing the same smoke contract as the CLI."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from search_quality.catalog import (
    DEFAULT_CATALOG_INDEX,
    CatalogSearchService,
    InvalidCatalogQuery,
)
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

app = FastAPI(
    title="Search Engine EVA Agent",
    version="0.2.0",
    description="Full-catalog baseline search and evaluation service",
)

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
            if request.url.path in {"/catalog/search", "/health", "/smoke"}
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
