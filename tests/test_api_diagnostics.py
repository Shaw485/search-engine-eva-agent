from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
from types import SimpleNamespace

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


class _FakeCatalogResult:
    def to_dict(self) -> dict:
        return {
            "backend": "sqlite-fts5",
            "hits": [],
            "index_id": "catalog-baseline-v1-0123456789ab",
            "locale_counts": {"us": 1},
            "product_count": 1,
        }


class _FakeCatalogService:
    metadata = SimpleNamespace(
        index_id="catalog-baseline-v1-0123456789ab",
        product_count=1,
    )

    def search(self, query: str, *, top_k: int):
        assert query == "private wireless mouse"
        assert top_k == 10
        return _FakeCatalogResult()


def test_catalog_search_returns_full_catalog_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "get_catalog_search_service", _FakeCatalogService)

    response = api.catalog_search_post(
        api.CatalogSearchRequest(query="private wireless mouse", top_k=10)
    )

    assert response == _FakeCatalogResult().to_dict()


def test_retrieval_analysis_endpoint_returns_stage_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "diagnosis_id": "stage-diagnosis-aaaaaaaaaaaa",
        "retrieval_run_id": "retrieval-bbbbbbbbbbbb",
        "status": "requires_engineering",
    }

    def analyze(**kwargs):
        assert kwargs["project_root"] == api.PROJECT_ROOT
        assert kwargs["profile_id"] == "smoke"
        return expected

    monkeypatch.setattr(api, "generate_retrieval_runtime_analysis", analyze)

    assert api.agent_retrieval_analyze(api.RetrievalAnalysisRequest()) == expected


def test_retrieval_analysis_route_has_a_strict_success_contract() -> None:
    route = next(
        item
        for item in api.app.routes
        if getattr(item, "path", None) == "/agent/retrieval/analyze"
    )
    assert route.response_model is api.RetrievalAnalysisResponse
    assert api.RetrievalAnalysisResponse.model_config["extra"] == "forbid"


def test_agent_eval_endpoint_returns_only_aggregate_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = {
        "task_success_rate": 1.0,
        "grounded_claim_rate": 1.0,
        "tool_selection_accuracy": 1.0,
        "recovery_rate": 1.0,
        "budget_compliance_rate": 1.0,
        "replay_fidelity_rate": 1.0,
        "tamper_rejection_rate": 1.0,
        "unauthorized_effect_count": 0,
        "protected_profile_read_count": 0,
        "strategy_write_count": 0,
        "total_agent_steps": 35,
        "total_agent_tool_calls": 27,
        "comparable_workflow_success_rate": 1.0,
        "comparable_workflow_tool_calls": 12,
    }
    evidence = SimpleNamespace(
        suite_id="stage5-retrieval-v1",
        evidence_id="agent-eval-aaaaaaaaaaaa",
        formal_passed=True,
        tasks=[object()] * 12,
        metrics=SimpleNamespace(model_dump=lambda **_kwargs: metrics),
        subject_summaries=(
            SimpleNamespace(
                subject_kind="production_planner", task_count=8, passed_count=8
            ),
            SimpleNamespace(
                subject_kind="harness_stimulus", task_count=4, passed_count=4
            ),
        ),
        limitations=(
            "scripted_failures_do_not_prove_worker_deadline_enforcement",
            "contract_fixtures_test_runtime_behavior_not_search_quality",
            "grounded_claim_rate_v1_is_terminal_grounding_proxy",
        ),
    )

    def run(**kwargs):
        assert kwargs["project_root"] == api.PROJECT_ROOT
        assert kwargs["suite_id"] == "stage5-retrieval-v1"
        assert kwargs["revision_provider"] is api._api_code_revision
        return SimpleNamespace(
            evidence=evidence,
            execution=SimpleNamespace(
                execution_id="agent-eval-execution-" + ("b" * 32)
            ),
        )

    monkeypatch.setattr(api, "run_agent_eval_suite", run)
    response = api.agent_eval_run(api.AgentEvalRequest())
    validated = api.AgentEvalResponse.model_validate(response, strict=True)

    assert validated.formal_passed is True
    assert validated.task_count == 12
    assert validated.metrics.comparable_workflow_success_rate == 1.0
    assert validated.subject_summaries[0].subject_kind == "production_planner"
    assert "tasks" not in response
    assert "query_text" not in json.dumps(response)

    contradictory = dict(response)
    contradictory["metrics"] = dict(response["metrics"])
    contradictory["metrics"]["strategy_write_count"] = 1
    with pytest.raises(ValueError, match="formal Agent Eval"):
        api.AgentEvalResponse.model_validate(contradictory, strict=True)

    wrong_attribution = dict(response)
    wrong_attribution["subject_summaries"] = (
        {"subject_kind": "production_planner", "task_count": 7, "passed_count": 7},
        {"subject_kind": "harness_stimulus", "task_count": 5, "passed_count": 5},
    )
    with pytest.raises(ValueError, match="Suite v1"):
        api.AgentEvalResponse.model_validate(wrong_attribution, strict=True)

    impossible_cost = dict(response)
    impossible_cost["metrics"] = dict(response["metrics"])
    impossible_cost["metrics"]["total_agent_steps"] = 26
    with pytest.raises(ValueError, match="tool calls exceed"):
        api.AgentEvalResponse.model_validate(impossible_cost, strict=True)


def test_agent_eval_rejects_concurrent_run() -> None:
    assert api._AGENT_EVAL_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as captured:
            api.agent_eval_run(api.AgentEvalRequest())
    finally:
        api._AGENT_EVAL_LOCK.release()

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "agent_eval_in_progress"


def test_agent_eval_failure_is_safe_and_omits_trace_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail(**_kwargs):
        raise ValueError("private query, raw Trace and /private/eval/path")

    monkeypatch.setattr(api, "run_agent_eval_suite", fail)
    with logging_context(trace_id="agent-eval-api-safe"):
        with pytest.raises(HTTPException) as captured:
            api.agent_eval_run(api.AgentEvalRequest())

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "agent_eval_unavailable",
        "message": "Agent evaluation unavailable",
        "trace_id": "agent-eval-api-safe",
    }
    log = stream.getvalue()
    assert "private query" not in log
    assert "raw Trace" not in log
    assert "/private/eval/path" not in log
    event = json.loads(log)
    assert event["event"] == "agent_eval_failed"
    assert event["error_type"] == "ValueError"


def test_query_constructor_endpoint_is_smoke_only_and_returns_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = SimpleNamespace(
        source_profile="smoke",
        query_set_id="query-set-cccccccccccc",
        query_count=59,
        original_count=20,
        synthetic_count=39,
        deduplicated_count=1,
        cases=(
            [SimpleNamespace(construction=SimpleNamespace(value="identity"))] * 20
            + [
                SimpleNamespace(
                    construction=SimpleNamespace(value="adjacent_transposition")
                )
            ]
            * 20
            + [
                SimpleNamespace(
                    construction=SimpleNamespace(value="token_order_reversal")
                )
            ]
            * 19
        ),
        formal_evaluation_allowed=False,
        locked_profiles_not_read=("dev", "test"),
        cross_split_collision_status="not_checked_without_reading_locked_splits",
    )
    stored: list[tuple[object, object]] = []

    def build(**kwargs):
        assert kwargs == {
            "project_root": api.PROJECT_ROOT,
            "revision_provider": api._api_code_revision,
            "source_profile": "smoke",
        }
        return artifact

    monkeypatch.setattr(api, "build_smoke_query_set", build)
    monkeypatch.setattr(
        api,
        "store_query_set",
        lambda value, **kwargs: stored.append((value, kwargs["artifact_root"])),
    )
    response = api.agent_query_constructor_build(api.QueryConstructorRequest())
    validated = api.QueryConstructorResponse.model_validate(response, strict=True)

    assert validated.query_count == 59
    assert validated.construction_counts.identity == 20
    assert validated.construction_counts.adjacent_transposition == 20
    assert validated.construction_counts.token_order_reversal == 19
    assert validated.formal_evaluation_allowed is False
    assert stored and stored[0][0] is artifact
    assert "cases" not in response

    contradictory = dict(response)
    contradictory["synthetic_count"] = 38
    with pytest.raises(ValueError, match="original plus synthetic"):
        api.QueryConstructorResponse.model_validate(contradictory, strict=True)


def test_query_constructor_failure_is_safe_and_omits_source_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail(**_kwargs):
        raise ValueError("private query, source row and /private/source/path")

    monkeypatch.setattr(api, "build_smoke_query_set", fail)
    with logging_context(trace_id="query-constructor-api-safe"):
        with pytest.raises(HTTPException) as captured:
            api.agent_query_constructor_build(api.QueryConstructorRequest())

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "query_constructor_unavailable",
        "message": "Query constructor unavailable",
        "trace_id": "query-constructor-api-safe",
    }
    log = stream.getvalue()
    assert "private query" not in log
    assert "source row" not in log
    assert "/private/source/path" not in log
    event = json.loads(log)
    assert event["event"] == "query_constructor_failed"
    assert event["error_type"] == "ValueError"


def test_new_agent_tool_requests_forbid_overrides() -> None:
    with pytest.raises(ValueError):
        api.AgentEvalRequest(suite="stage5-retrieval-v1", profile="test")
    with pytest.raises(ValueError):
        api.QueryConstructorRequest(source="smoke", source_path="/private/data")


def test_runtime_response_rejects_two_failed_attempts_before_success() -> None:
    baseline = {
        "evidence_ref": "run:retrieval-aaaaaaaaaaaa",
        "failed_gates": [],
        "gate_passed": None,
        "pipeline_variant": None,
        "reason_code": "diagnose_retrieval_baseline",
        "retryable": False,
        "sequence": 1,
        "status": "succeeded",
        "tool_name": "diagnose_baseline_retrieval",
    }
    failed = {
        "evidence_ref": None,
        "failed_gates": [],
        "gate_passed": None,
        "pipeline_variant": "title-exact-multifield-v1",
        "reason_code": "test_uniform_multifield_fusion",
        "retryable": True,
        "status": "failed",
        "tool_name": "run_retrieval_candidate",
    }
    succeeded = {
        **failed,
        "evidence_ref": "comparison:retrieval-comparison-bbbbbbbbbbbb",
        "gate_passed": False,
        "failed_gates": ["fusion_mrr_at_10_floor"],
        "retryable": False,
        "status": "succeeded",
    }
    with pytest.raises(ValueError, match="failed action retry"):
        api.RetrievalAgentRunResponse.model_validate(
            {
                "actions": [
                    baseline,
                    {**failed, "sequence": 2},
                    {**failed, "sequence": 3},
                    {**succeeded, "sequence": 4},
                ],
                "outcome": "proposal_ready",
                "planner_id": "stage-aware-retrieval-planner-v1",
                "reason_code": "conservative_candidate_selected",
                "replay_supported": True,
                "runtime_id": "search-agent-runtime-v1",
                "schema_version": "retrieval-agent-run-summary-v1",
                "state": "completed",
                "steps_used": 5,
                "tool_calls_used": 4,
                "trace_id": "c" * 32,
            },
            strict=True,
        )


def test_retrieval_analysis_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail(**_kwargs):
        raise ValueError("private query, product title and /private/path")

    monkeypatch.setattr(api, "generate_retrieval_runtime_analysis", fail)
    with logging_context(trace_id="retrieval-api-safe"):
        with pytest.raises(HTTPException) as captured:
            api.agent_retrieval_analyze(api.RetrievalAnalysisRequest())

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "retrieval_analysis_unavailable",
        "message": "Retrieval analysis workflow unavailable",
        "trace_id": "retrieval-api-safe",
    }
    assert "private query" not in stream.getvalue()
    assert "product title" not in stream.getvalue()
    assert "/private/path" not in stream.getvalue()


def test_catalog_search_failure_is_safe_and_does_not_log_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    class FailingCatalogService:
        def search(self, _query: str, *, top_k: int):
            assert top_k == 10
            raise sqlite3.DatabaseError("private query and index path")

    monkeypatch.setattr(
        api,
        "get_catalog_search_service",
        lambda: FailingCatalogService(),
    )
    with logging_context(trace_id="catalog-safe-1"):
        with pytest.raises(HTTPException) as captured:
            api.catalog_search_post(
                api.CatalogSearchRequest(query="private query", top_k=10)
            )

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "catalog_search_unavailable",
        "message": "Catalog search unavailable",
        "trace_id": "catalog-safe-1",
    }
    assert "private query" not in stream.getvalue()
    assert "index path" not in stream.getvalue()
    event = json.loads(stream.getvalue())
    assert event["event"] == "catalog_search_failed"
    assert event["error_type"] == "DatabaseError"


def test_invalid_catalog_query_returns_safe_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RejectingCatalogService:
        def search(self, _query: str, *, top_k: int):
            raise api.InvalidCatalogQuery("private invalid input")

    monkeypatch.setattr(
        api,
        "get_catalog_search_service",
        lambda: RejectingCatalogService(),
    )
    with logging_context(trace_id="catalog-safe-2"):
        with pytest.raises(HTTPException) as captured:
            api.catalog_search_post(api.CatalogSearchRequest(query="___", top_k=10))

    assert captured.value.status_code == 400
    assert captured.value.detail == {
        "code": "invalid_catalog_query",
        "message": "Search query is invalid",
        "trace_id": "catalog-safe-2",
    }
    assert "private invalid input" not in json.dumps(captured.value.detail)


def test_catalog_health_distinguishes_ready_and_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "get_catalog_search_service", _FakeCatalogService)
    assert api.health() == {
        "catalog": {
            "index_id": "catalog-baseline-v1-0123456789ab",
            "product_count": 1,
            "status": "ready",
        },
        "stage": "catalog-baseline",
        "status": "ok",
    }

    def unavailable():
        raise FileNotFoundError("private index path")

    monkeypatch.setattr(api, "get_catalog_search_service", unavailable)
    assert api.health() == {
        "catalog": {"status": "unavailable"},
        "stage": "catalog-baseline",
        "status": "ok",
    }


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
