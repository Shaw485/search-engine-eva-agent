"""Small Stage 0 API exposing the same smoke contract as the CLI."""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query

from search_quality.smoke import run_smoke

app = FastAPI(
    title="Search Engine EVA Agent",
    version="0.1.0",
    description="Stage 0 search backend smoke service",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "stage": "0"}


@app.get("/smoke")
def smoke(
    query: str = Query(default="wireless mouse", min_length=1, max_length=200),
    top_k: int = Query(default=5, ge=1, le=10),
    backend: str = Query(default=os.environ.get("SEARCH_BACKEND", "local")),
) -> dict:
    if backend not in {"local", "opensearch"}:
        raise HTTPException(status_code=400, detail="unsupported backend")
    try:
        return run_smoke(backend_name=backend, query=query, top_k=top_k)
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
