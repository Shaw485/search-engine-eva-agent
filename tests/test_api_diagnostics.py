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
from search_quality.bad_cases.contracts import (
    BadCaseCategoryCounts,
    BadCaseDisplayHit,
    BadCaseSample,
)
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


def _fake_bad_case_run():
    sample = BadCaseSample(
        case_id="query-case-aaaaaaaaaaaa",
        source_case_id="query-case-bbbbbbbbbbbb",
        construction="adjacent_transposition",
        categories=["zero_result", "spelling_sensitive"],
        reason_code="variant_zero_result",
        query_text="wieeless mouse",
        source_query_text="wireless mouse",
        source_returned_at_k=1,
        variant_returned_at_k=0,
        overlap_at_k=0,
        source_top_hits=[
            BadCaseDisplayHit(
                product_id="B000EXACT1",
                locale="us",
                title="Private readable product",
                rank=1,
            )
        ],
        variant_top_hits=[],
    )
    artifact = SimpleNamespace(
        completed=True,
        diagnostic_id="bad-case-aaaaaaaaaaaa",
        query_set_id="query-set-bbbbbbbbbbbb",
        index_id="catalog-baseline-v1-cccccccccccc",
        search_strategy_id="sqlite-fts5-bm25",
        query_count=59,
        original_count=20,
        synthetic_count=39,
        construction_counts={
            "identity": 20,
            "adjacent_transposition": 20,
            "token_order_reversal": 19,
        },
        top_k=10,
        search_call_count=59,
        operational_failure_count=0,
        diagnostic_candidate_count=1,
        category_counts=BadCaseCategoryCounts(
            zero_result=1,
            spelling_sensitive=1,
            order_sensitive=0,
            ranking_instability_needs_judgment=0,
        ),
        relevance_labels_used=False,
        relevance_metrics_computed=False,
        quality_metrics_computed=False,
        formal_evaluation_allowed=False,
        stage_drop_diagnostics_computed=False,
        locked_profiles_not_read=("dev", "test"),
        protected_profile_dispatch_count=0,
        strategy_write_count=0,
        limitations=(
            "synthetic_queries_are_unjudged",
            "diagnostics_do_not_claim_relevance_improvement",
            "development_smoke_is_not_final_evaluation",
            "single_stage_catalog_cannot_diagnose_stage_drop",
            "no_hard_worker_deadline_enforcement",
        ),
    )
    return SimpleNamespace(
        artifact=artifact,
        execution=SimpleNamespace(execution_id="bad-case-execution-" + ("d" * 32)),
        samples=[sample],
    )


def test_bad_case_endpoint_runs_fixed_batch_and_returns_limited_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _fake_bad_case_run()

    def run(**kwargs):
        assert kwargs["project_root"] == api.PROJECT_ROOT
        assert kwargs["artifact_root"] is None
        assert kwargs["catalog_index_path"] == "trusted-index"
        assert kwargs["executor_revision"] == "a" * 40
        assert kwargs["source_profile"] == "smoke"
        assert kwargs["deadline_ms"] == 125_000
        assert isinstance(kwargs["trace_id"], str)
        return expected

    monkeypatch.setattr(api, "_agent_artifact_root", lambda: None)
    monkeypatch.setattr(api, "_catalog_index_path", lambda: "trusted-index")
    monkeypatch.setattr(api, "_api_code_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(api, "supervise_bad_case_diagnostics", run)

    def load_receipt(root, execution_id):
        assert root == api.PROJECT_ROOT / "runs"
        assert execution_id == "bad-case-execution-" + ("d" * 32)
        return SimpleNamespace(
            receipt_id="bad-case-supervisor-execution-eeeeeeeeeeee",
            execution_id=execution_id,
            diagnostic_id="bad-case-aaaaaaaaaaaa",
            policy_id="posix-process-group-deadline-v1",
            deadline_ms=125_000,
            term_grace_ms=1_000,
            kill_grace_ms=1_000,
            completion_observation="worker_result",
            completed=True,
        )

    monkeypatch.setattr(api, "load_supervisor_execution_receipt", load_receipt)
    response = api.agent_bad_cases_run(api.BadCaseRunRequest())
    validated = api.BadCaseRunResponse.model_validate(response, strict=True)

    assert validated.completed is True
    assert validated.query_count == 59
    assert validated.search_call_count == 59
    assert validated.operational_failure_count == 0
    assert validated.diagnostic_candidate_count == 1
    assert validated.samples[0].query_text == "wieeless mouse"
    assert validated.samples[0].source_top_hits[0].title == ("Private readable product")
    assert validated.relevance_labels_used is False
    assert validated.quality_metrics_computed is False
    assert validated.stage_drop_diagnostics_computed is False
    assert validated.worker_hard_deadline_enforced is True
    assert validated.worker_deadline_ms == 125_000
    assert (
        validated.supervisor_receipt_id == "bad-case-supervisor-execution-eeeeeeeeeeee"
    )
    assert validated.term_grace_ms == 1_000
    assert validated.kill_grace_ms == 1_000
    assert validated.completion_observation == "worker_result"

    contradictory = dict(response)
    contradictory["diagnostic_candidate_count"] = 3
    with pytest.raises(ValueError, match="category totals"):
        api.BadCaseRunResponse.model_validate(contradictory, strict=True)

    duplicate = dict(response)
    duplicate["samples"] = response["samples"] * 2
    duplicate["diagnostic_candidate_count"] = 2
    duplicate["category_counts"] = {
        "zero_result": 2,
        "spelling_sensitive": 2,
        "order_sensitive": 0,
        "ranking_instability_needs_judgment": 0,
    }
    with pytest.raises(ValueError, match="unique"):
        api.BadCaseRunResponse.model_validate(duplicate, strict=True)


def test_bad_case_endpoint_rejects_a_contradictory_supervisor_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _fake_bad_case_run()
    monkeypatch.setattr(api, "_agent_artifact_root", lambda: None)
    monkeypatch.setattr(api, "_catalog_index_path", lambda: "trusted-index")
    monkeypatch.setattr(api, "_api_code_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(
        api, "supervise_bad_case_diagnostics", lambda **_kwargs: expected
    )
    monkeypatch.setattr(
        api,
        "load_supervisor_execution_receipt",
        lambda _root, execution_id: SimpleNamespace(
            receipt_id="bad-case-supervisor-execution-eeeeeeeeeeee",
            execution_id=execution_id,
            diagnostic_id="bad-case-ffffffffffff",
            policy_id="posix-process-group-deadline-v1",
            deadline_ms=125_000,
            term_grace_ms=1_000,
            kill_grace_ms=1_000,
            completion_observation="worker_result",
            completed=True,
        ),
    )

    with logging_context(trace_id="bad-case-receipt-conflict"):
        with pytest.raises(HTTPException) as captured:
            api.agent_bad_cases_run(api.BadCaseRunRequest())

    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "bad_case_run_unavailable"


def test_bad_case_endpoint_rejects_concurrent_run() -> None:
    assert api._BAD_CASE_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as captured:
            api.agent_bad_cases_run(api.BadCaseRunRequest())
    finally:
        api._BAD_CASE_LOCK.release()
    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "bad_case_run_in_progress"


def test_bad_case_failure_is_safe_and_omits_query_and_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail(**_kwargs):
        raise ValueError("private Query and private product title")

    monkeypatch.setattr(api, "_catalog_index_path", lambda: "trusted-index")
    monkeypatch.setattr(api, "_api_code_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(api, "supervise_bad_case_diagnostics", fail)
    with logging_context(trace_id="bad-case-api-safe"):
        with pytest.raises(HTTPException) as captured:
            api.agent_bad_cases_run(api.BadCaseRunRequest())
    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "bad_case_run_unavailable",
        "message": "Bad Case diagnostics unavailable",
        "trace_id": "bad-case-api-safe",
    }
    assert "private Query" not in stream.getvalue()
    assert "private product title" not in stream.getvalue()
    assert "bad_case_run_failed" in stream.getvalue()


def test_bad_case_worker_deadline_returns_504_with_execution_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = "bad-case-execution-" + ("e" * 32)

    def timeout(**_kwargs):
        raise api.BadCaseWorkerDeadlineExceeded(
            "private worker detail",
            execution_id=execution_id,
            error_code="worker_deadline_exceeded",
        )

    monkeypatch.setattr(api, "_catalog_index_path", lambda: "trusted-index")
    monkeypatch.setattr(api, "_api_code_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(api, "supervise_bad_case_diagnostics", timeout)
    with logging_context(trace_id="bad-case-deadline-safe"):
        with pytest.raises(HTTPException) as captured:
            api.agent_bad_cases_run(api.BadCaseRunRequest())

    assert captured.value.status_code == 504
    assert captured.value.detail == {
        "code": "bad_case_worker_deadline_exceeded",
        "message": "Bad Case diagnostics exceeded the worker deadline",
        "trace_id": "bad-case-deadline-safe",
        "execution_id": execution_id,
    }


def test_diagnostic_experiment_plan_loads_only_the_requested_evidence_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = object()
    plan = object()

    def load(**kwargs):
        assert kwargs == {
            "artifact_root": api.PROJECT_ROOT / "runs",
            "diagnostic_id": "bad-case-aaaaaaaaaaaa",
            "query_set_id": "query-set-bbbbbbbbbbbb",
        }
        return evidence

    def route(actual):
        assert actual is evidence
        return plan

    monkeypatch.setattr(api, "_agent_artifact_root", lambda: None)
    monkeypatch.setattr(api, "load_resolved_diagnostic_evidence", load)
    monkeypatch.setattr(api, "route_diagnostic_evidence", route)

    response = api.agent_diagnostic_experiment_plan(
        api.DiagnosticExperimentPlanRequest(
            diagnostic_id="bad-case-aaaaaaaaaaaa",
            query_set_id="query-set-bbbbbbbbbbbb",
        )
    )
    assert response is plan


def test_diagnostic_experiment_route_has_strict_contracts() -> None:
    route = next(
        item
        for item in api.app.routes
        if getattr(item, "path", None) == "/agent/diagnostic-experiments/plan"
    )
    assert route.response_model is api.DiagnosticExperimentPlan
    assert api.DiagnosticExperimentPlanRequest.model_config["extra"] == "forbid"
    with pytest.raises(ValueError):
        api.DiagnosticExperimentPlanRequest(
            diagnostic_id="bad-case-aaaaaaaaaaaa",
            query_set_id="query-set-bbbbbbbbbbbb",
            artifact_path="/private/run.json",
        )


def test_diagnostic_experiment_plan_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail(**_kwargs):
        raise ValueError("private Query and /private/evidence/path")

    monkeypatch.setattr(api, "load_resolved_diagnostic_evidence", fail)
    with logging_context(trace_id="diagnostic-plan-api-safe"):
        with pytest.raises(HTTPException) as captured:
            api.agent_diagnostic_experiment_plan(
                api.DiagnosticExperimentPlanRequest(
                    diagnostic_id="bad-case-aaaaaaaaaaaa",
                    query_set_id="query-set-bbbbbbbbbbbb",
                )
            )
    assert captured.value.status_code == 409
    assert captured.value.detail == {
        "code": "diagnostic_evidence_unavailable",
        "message": "Diagnostic evidence is unavailable or stale",
        "trace_id": "diagnostic-plan-api-safe",
    }
    assert "private Query" not in stream.getvalue()
    assert "/private/evidence/path" not in stream.getvalue()
    assert "diagnostic_experiment_plan_failed" in stream.getvalue()


def test_new_agent_tool_requests_forbid_overrides() -> None:
    with pytest.raises(ValueError):
        api.AgentEvalRequest(suite="stage5-retrieval-v1", profile="test")
    with pytest.raises(ValueError):
        api.QueryConstructorRequest(source="smoke", source_path="/private/data")
    with pytest.raises(ValueError):
        api.BadCaseRunRequest(source="smoke", query_set_id="query-set-private")


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
