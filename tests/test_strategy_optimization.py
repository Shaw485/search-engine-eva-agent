from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.api import main as api
from search_quality.agent.optimization import (
    apply_strategy_decision,
    generate_strategy_proposal,
    load_strategy_catalog,
)
from search_quality.observability import configure_logging, logging_context

PROJECT_ROOT = Path(__file__).parents[1]


def _request(
    *,
    client: tuple[str, int] = ("127.0.0.1", 50000),
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/strategy/decision",
            "headers": headers or [],
            "client": client,
        }
    )


def _copy_smoke_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "configs" / "evaluation").mkdir(parents=True)
    (project / "data" / "manifests").mkdir(parents=True)
    (project / "data" / "samples").mkdir(parents=True)
    shutil.copy2(
        PROJECT_ROOT / "configs" / "evaluation" / "esci-primary-v1.json",
        project / "configs" / "evaluation" / "esci-primary-v1.json",
    )
    shutil.copy2(
        PROJECT_ROOT / "data" / "manifests" / "esci-stage1.json",
        project / "data" / "manifests" / "esci-stage1.json",
    )
    shutil.copy2(
        PROJECT_ROOT / "data" / "samples" / "esci-stage1-smoke.parquet",
        project / "data" / "samples" / "esci-stage1-smoke.parquet",
    )
    return project


def test_agent_generates_real_strategy_proposal_artifacts(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)

    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    assert proposal["schema_version"] == "strategy-proposal-v1"
    assert proposal["status"] == "pending"
    assert proposal["agent_summary"]["recommendation"] == "update_strategy"
    assert proposal["strategy"]["strategy_id"] == "candidate-title-bm25-exact-boost-v1"
    assert proposal["evidence"]["aggregate_metrics"]["ndcg@10"]["delta"] > 0
    assert proposal["evidence"]["bad_cases"]
    assert (
        project / "runs" / "strategy-proposals" / f"{proposal['proposal_id']}.json"
    ).is_file()


def test_agent_artifacts_can_live_outside_read_only_project(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    artifact_root = tmp_path / "runtime"
    artifact_root.mkdir()

    proposal = generate_strategy_proposal(
        project_root=project,
        artifact_root=artifact_root,
        revision_provider=lambda _root: "a" * 40,
    )
    decision = apply_strategy_decision(
        project_root=project,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )
    catalog = load_strategy_catalog(
        project_root=project,
        artifact_root=artifact_root,
    )

    assert not (project / "runs").exists()
    assert (
        artifact_root / "strategy-proposals" / f"{proposal['proposal_id']}.json"
    ).is_file()
    assert (artifact_root / "search-strategies" / "active.json").is_file()
    assert decision["active_strategy_path"] == "runs/search-strategies/active.json"
    assert catalog["active_strategy_id"] == "candidate-title-bm25-exact-boost-v1"


def test_strategy_decision_approve_updates_catalog_and_is_idempotent(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    decision = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )
    second = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )

    catalog = load_strategy_catalog(project_root=project)
    assert second == decision
    assert decision["applied"] is True
    assert decision["active_strategy_path"] == "runs/search-strategies/active.json"
    assert catalog["active_strategy_id"] == "candidate-title-bm25-exact-boost-v1"
    assert [item["strategy_id"] for item in catalog["strategies"]] == [
        "candidate-title-bm25-exact-boost-v1"
    ]
    with pytest.raises(ValueError, match="different decision"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="reject",
        )


def test_strategy_decision_reject_records_without_updating_catalog(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    decision = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="reject",
    )

    assert decision["applied"] is False
    assert load_strategy_catalog(project_root=project)["strategies"] == []


def test_api_strategy_routes_return_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_AGENT_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("SEARCH_CODE_REVISION", raising=False)
    monkeypatch.setattr(
        api,
        "generate_strategy_proposal",
        lambda **_kwargs: {"proposal_id": "proposal-aaaaaaaaaaaa"},
    )
    monkeypatch.setattr(
        api,
        "apply_strategy_decision",
        lambda **_kwargs: {"decision_id": "decision-bbbbbbbbbbbb"},
    )
    monkeypatch.setattr(
        api,
        "load_strategy_catalog",
        lambda **_kwargs: {"schema_version": "search-strategy-catalog-v1"},
    )

    assert api.agent_strategy_propose(api.StrategyProposalRequest()) == {
        "proposal_id": "proposal-aaaaaaaaaaaa"
    }
    assert api.agent_strategy_decision(
        _request(),
        api.StrategyDecisionRequest(
            proposal_id="proposal-aaaaaaaaaaaa",
            decision="approve",
        ),
    ) == {"decision_id": "decision-bbbbbbbbbbbb"}
    assert api.agent_strategy_catalog() == {
        "schema_version": "search-strategy-catalog-v1"
    }


def test_api_uses_external_artifacts_and_deployment_revision_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "runtime"
    artifact_root.mkdir()
    revision = "b" * 40
    calls: list[dict] = []
    monkeypatch.setenv("SEARCH_AGENT_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("SEARCH_CODE_REVISION", revision)
    api._AGENT_PROPOSAL_CACHE.clear()

    def proposal(**kwargs):
        calls.append(kwargs)
        return {"proposal_id": "proposal-bbbbbbbbbbbb"}

    monkeypatch.setattr(api, "generate_strategy_proposal", proposal)

    first = api.agent_strategy_propose(api.StrategyProposalRequest())
    second = api.agent_strategy_propose(api.StrategyProposalRequest())

    assert first == second == {"proposal_id": "proposal-bbbbbbbbbbbb"}
    assert len(calls) == 1
    assert calls[0]["artifact_root"] == artifact_root.resolve()
    assert calls[0]["revision_provider"](PROJECT_ROOT) == revision


def test_api_rejects_public_strategy_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def decision(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(api, "apply_strategy_decision", decision)
    public_request = _request(
        headers=[(b"x-forwarded-for", b"203.0.113.10")],
    )
    with pytest.raises(HTTPException) as captured:
        api.agent_strategy_decision(
            public_request,
            api.StrategyDecisionRequest(
                proposal_id="proposal-aaaaaaaaaaaa",
                decision="approve",
            ),
        )

    assert captured.value.status_code == 404
    assert called is False


def test_api_strategy_proposal_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail_proposal(**_kwargs):
        raise RuntimeError("private strategy detail")

    monkeypatch.setattr(api, "generate_strategy_proposal", fail_proposal)
    with logging_context(trace_id="strategy-safe-1"):
        with pytest.raises(HTTPException) as captured:
            api.agent_strategy_propose(api.StrategyProposalRequest())

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "strategy_proposal_unavailable",
        "message": "Strategy proposal workflow unavailable",
        "trace_id": "strategy-safe-1",
    }
    assert "private strategy detail" not in stream.getvalue()
    event = json.loads(stream.getvalue())
    assert event["event"] == "agent_strategy_proposal_failed"
    assert event["error_type"] == "RuntimeError"


def test_strategy_optimizer_logs_without_query_text(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"agent_optimization": "INFO"},
        stream=stream,
    )

    with logging_context(trace_id="strategy-trace"):
        proposal = generate_strategy_proposal(
            project_root=project,
            revision_provider=lambda _root: "a" * 40,
        )

    assert proposal["evidence"]["bad_cases"]
    assert "07 nissan" not in stream.getvalue()
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "strategy_proposal_started",
        "strategy_proposal_completed",
    ]
    assert all(event["trace_id"] == "strategy-trace" for event in events)
