from __future__ import annotations

import asyncio
import io
import json
import logging

import anyio
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from apps.api import main as api
from search_quality.observability import (
    configure_logging,
    current_trace_id,
    logging_context,
)


def test_public_smoke_failure_is_correlated_without_leaking_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail_smoke(**_kwargs):
        raise ValueError("private query and backend response must stay internal")

    monkeypatch.setattr(api, "run_smoke", fail_smoke)
    with logging_context(trace_id="request-safe-1"):
        with pytest.raises(HTTPException) as captured:
            api.smoke(query="private query", top_k=5, backend="local")

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "search_backend_unavailable",
        "message": "Search backend unavailable",
        "trace_id": "request-safe-1",
    }
    assert "private query" not in json.dumps(captured.value.detail)
    assert "backend response" not in stream.getvalue()
    event = json.loads(stream.getvalue())
    assert event["event"] == "smoke_search_failed"
    assert event["error_type"] == "ValueError"


def test_request_diagnostics_omits_query_string_and_returns_trace_id() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "INFO"},
        stream=stream,
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/smoke",
        "raw_path": b"/smoke",
        "query_string": b"query=private-search-text",
        "headers": [(b"x-request-id", b"Bearer-private-header-secret")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8010),
    }
    request = Request(scope)

    async def call_next(_request: Request) -> Response:
        return Response("ok", status_code=200)

    original_new_trace_id = api.new_trace_id
    api.new_trace_id = lambda: "request-safe-2"
    try:
        response = asyncio.run(api.request_diagnostics(request, call_next))
    finally:
        api.new_trace_id = original_new_trace_id
    assert response.headers["X-Request-ID"] == "request-safe-2"
    assert "private-search-text" not in stream.getvalue()
    assert "private-header-secret" not in stream.getvalue()
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["request_completed"]
    assert all(event["route"] == "/smoke" for event in events)


def test_post_smoke_propagates_one_trace_across_sync_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "INFO", "backend": "INFO"},
        stream=stream,
    )

    def fail_smoke(**_kwargs):
        assert current_trace_id() == "request-safe-3"
        logging.getLogger("search_quality.backend").error(
            "backend_probe_failed",
            extra={"error_code": "probe_failure", "error_type": "ValueError"},
        )
        raise ValueError("private backend cause")

    monkeypatch.setattr(api, "run_smoke", fail_smoke)
    monkeypatch.setattr(api, "new_trace_id", lambda: "request-safe-3")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/smoke",
        "raw_path": b"/smoke",
        "query_string": b"",
        "headers": [(b"x-request-id", b"incoming-private-token")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8010),
    }
    request = Request(scope)

    async def call_next(_request: Request) -> Response:
        try:
            return await anyio.to_thread.run_sync(
                lambda: JSONResponse(
                    api.smoke_post(
                        api.SmokeRequest(
                            query="private query", top_k=5, backend="local"
                        )
                    )
                )
            )
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )

    response = asyncio.run(api.request_diagnostics(request, call_next))
    body = json.loads(response.body)
    assert response.status_code == 503
    assert response.headers["X-Request-ID"] == "request-safe-3"
    assert body["detail"]["trace_id"] == "request-safe-3"
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    traced = [event for event in events if "trace_id" in event]
    assert traced
    assert all(event["trace_id"] == "request-safe-3" for event in traced)
    assert "private query" not in stream.getvalue()
    assert "private backend cause" not in stream.getvalue()
    assert "incoming-private-token" not in stream.getvalue()


def test_unhandled_failure_returns_safe_trace_and_redacts_unknown_path() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "INFO"},
        stream=stream,
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/private-path-secret",
        "raw_path": b"/private-path-secret",
        "query_string": b"query=private-query-secret",
        "headers": [(b"x-request-id", b"request-safe-4")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8010),
    }
    request = Request(scope)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("private exception secret")

    original_new_trace_id = api.new_trace_id
    api.new_trace_id = lambda: "request-safe-4"
    try:
        response = asyncio.run(api.request_diagnostics(request, call_next))
    finally:
        api.new_trace_id = original_new_trace_id
    body = json.loads(response.body)
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "request-safe-4"
    assert body["detail"] == {
        "code": "internal_server_error",
        "message": "Internal server error",
        "trace_id": "request-safe-4",
    }
    event = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "request_failed"
    ][0]
    assert event["route"] == "unmatched"
    assert event["error_code"] == "runtime_guard_failed"
    assert "private-path-secret" not in stream.getvalue()
    assert "private-query-secret" not in stream.getvalue()
    assert "private exception secret" not in stream.getvalue()
