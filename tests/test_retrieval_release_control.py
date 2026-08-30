from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import search_quality.agent.retrieval_release_control as release_control
from search_quality.agent.retrieval_release_control import (
    RetrievalReleaseError,
    apply_retrieval_release_decision,
    build_retrieval_validation_failure_receipt,
    create_or_load_retrieval_proposal,
    load_retrieval_activation_envelope,
    load_retrieval_release,
    load_retrieval_release_catalog,
    record_retrieval_release_outcome,
    record_retrieval_release_rollback,
)
from search_quality.agent.retrieval_runtime import (
    generate_retrieval_runtime_analysis,
)
from search_quality.catalog.pipeline_v2 import (
    PRODUCTION_PIPELINE_CONFIG,
    PRODUCTION_PIPELINE_CONFIG_SHA256,
    PRODUCTION_PIPELINE_ID,
)
from search_quality.catalog.serving import _validate_activation_envelope
from search_quality.observability import configure_logging, logging_context

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c" * 40


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(scope="module")
def retrieval_analysis_fixture(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, dict[str, Any]]:
    artifact_root = tmp_path_factory.mktemp("retrieval-release-evidence")
    analysis = generate_retrieval_runtime_analysis(
        project_root=ROOT,
        artifact_root=artifact_root,
        revision_provider=lambda _root: REVISION,
    )
    return artifact_root, analysis


@pytest.fixture
def release_case(
    tmp_path: Path,
    retrieval_analysis_fixture: tuple[Path, dict[str, Any]],
) -> tuple[Path, dict[str, Any]]:
    source, analysis = retrieval_analysis_fixture
    artifact_root = tmp_path / "runtime"
    shutil.copytree(source, artifact_root)
    return artifact_root, copy.deepcopy(analysis)


def _proposal(artifact_root: Path, analysis: dict[str, Any]) -> dict[str, Any]:
    return create_or_load_retrieval_proposal(
        analysis,
        project_root=ROOT,
        artifact_root=artifact_root,
        revision_provider=lambda _root: REVISION,
    )


def _approve(artifact_root: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    return apply_retrieval_release_decision(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        decision="approve",
        client_action_id="owner-action-001",
        actor_id="owner",
        revision_provider=lambda _root: REVISION,
    )


def _activation_receipt(
    proposal: dict[str, Any],
    *,
    strategy_revision: str = "a" * 64,
) -> dict[str, Any]:
    body = {
        "active": True,
        "channel_counts": {
            "title": 20,
            "exact": 4,
            "multi_field": 18,
            "union": 24,
            "fused": 20,
            "coarse": 10,
        },
        "code_revision": proposal["code_revision"],
        "config_sha256": proposal["selected_pipeline"]["config_sha256"],
        "index_id": "catalog-v2-0123456789ab",
        "index_schema_version": "catalog-sqlite-fts5-v2",
        "latency_ms": {"max": 3.0, "p95": 2.0, "total": 5.0},
        "parent_active_revision": proposal["parent_active_revision"],
        "passed": True,
        "pipeline_id": proposal["selected_pipeline"]["pipeline_id"],
        "previous_strategy_revision": proposal["parent_active_revision"],
        "proposal_id": proposal["proposal_id"],
        "proposal_revision": proposal["proposal_revision"],
        "rollback_strategy_revision": "b" * 64,
        "schema_version": "retrieval-serving-activation-receipt-v1",
        "sentinel_count": 2,
        "strategy_id": proposal["selected_pipeline"]["strategy_id"],
        "strategy_revision": strategy_revision,
    }
    return {
        **body,
        "receipt_id": f"activation-{_sha256_payload(body)[:12]}",
    }


def _rollback_receipt(
    *,
    from_strategy_revision: str,
    target_strategy_revision: str,
) -> dict[str, Any]:
    body = {
        "from_strategy_revision": from_strategy_revision,
        "index_id": "catalog-baseline-v1-0123456789ab",
        "index_schema_version": "catalog-sqlite-fts5-v1",
        "mode": "baseline",
        "schema_version": "retrieval-serving-rollback-receipt-v1",
        "strategy_id": "catalog-baseline-v1",
        "strategy_revision": target_strategy_revision,
        "succeeded": True,
    }
    return {
        **body,
        "receipt_id": f"rollback-{_sha256_payload(body)[:12]}",
    }


def test_formal_proposal_is_complete_immutable_and_idempotent(
    release_case: tuple[Path, dict[str, Any]],
) -> None:
    artifact_root, analysis = release_case

    first = _proposal(artifact_root, analysis)
    second = _proposal(artifact_root, analysis)

    assert first == second
    assert first["proposal_id"] == (
        f"retrieval-proposal-{first['proposal_revision'][:12]}"
    )
    assert len(first["proposal_revision"]) == 64
    assert first["lifecycle"] == "pending_owner_review"
    assert first["approval_eligible"] is True
    assert first["code_revision"] == REVISION
    assert first["parent_active_revision"] is None
    assert first["selected_pipeline"] == {
        "strategy_id": "multi-field-bm25-weighted-rrf-v1",
        "pipeline_id": PRODUCTION_PIPELINE_ID,
        "config": PRODUCTION_PIPELINE_CONFIG,
        "config_sha256": PRODUCTION_PIPELINE_CONFIG_SHA256,
    }
    assert first["evidence"] == {
        "baseline_run_id": analysis["retrieval_run_id"],
        "candidate_run_id": analysis["candidate_run_id"],
        "comparison_id": analysis["comparison_id"],
        "baseline_diagnosis_id": analysis["diagnosis_id"],
        "candidate_diagnosis_id": analysis["candidate_diagnosis_id"],
        "trace_id": analysis["agent_run"]["trace_id"],
    }
    assert first["release_gate"]["passed"] is True
    assert len(first["release_gate"]["checks"]) == 12
    stored = json.loads(
        (
            artifact_root
            / "retrieval-release-proposals"
            / f"{first['proposal_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert stored == first
    assert not (artifact_root / "retrieval-strategies").exists()
    assert not (artifact_root / "strategy-proposals").exists()


def test_proposal_rejects_analysis_or_trace_reference_tampering(
    release_case: tuple[Path, dict[str, Any]],
) -> None:
    artifact_root, analysis = release_case
    analysis["comparison_id"] = "retrieval-comparison-000000000000"

    with pytest.raises(RetrievalReleaseError) as caught:
        _proposal(artifact_root, analysis)

    assert caught.value.code in {"artifact_unavailable", "analysis_evidence_mismatch"}
    assert not (artifact_root / "retrieval-release-proposals").exists()


def test_owner_approval_is_idempotent_and_stops_before_activation(
    release_case: tuple[Path, dict[str, Any]],
) -> None:
    artifact_root, analysis = release_case
    proposal = _proposal(artifact_root, analysis)

    first = _approve(artifact_root, proposal)
    second = _approve(artifact_root, proposal)

    assert first == second
    assert first["decision"] == "approve"
    assert first["lifecycle"] == "approved_for_validation"
    assert first["validation_required"] is True
    assert first["activation_status"] == "not_active"
    assert first["proposal_revision"] == proposal["proposal_revision"]
    assert not (artifact_root / "retrieval-strategies" / "active.json").exists()
    loaded = load_retrieval_release(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
    )
    assert loaded["lifecycle"] == "approved_for_validation"
    envelope = load_retrieval_activation_envelope(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
    )
    assert _validate_activation_envelope(envelope)["proposal"] == proposal


def test_decision_conflicts_are_rejected_by_revision_and_client_action(
    release_case: tuple[Path, dict[str, Any]],
) -> None:
    artifact_root, analysis = release_case
    proposal = _proposal(artifact_root, analysis)

    with pytest.raises(RetrievalReleaseError) as stale:
        apply_retrieval_release_decision(
            project_root=ROOT,
            artifact_root=artifact_root,
            proposal_id=proposal["proposal_id"],
            proposal_revision="f" * 64,
            decision="approve",
            client_action_id="owner-action-stale",
            actor_id="owner",
            revision_provider=lambda _root: REVISION,
        )
    assert stale.value.code == "proposal_revision_conflict"

    _approve(artifact_root, proposal)
    with pytest.raises(RetrievalReleaseError) as conflicting:
        apply_retrieval_release_decision(
            project_root=ROOT,
            artifact_root=artifact_root,
            proposal_id=proposal["proposal_id"],
            proposal_revision=proposal["proposal_revision"],
            decision="reject",
            client_action_id="owner-action-002",
            actor_id="owner",
            revision_provider=lambda _root: REVISION,
        )
    assert conflicting.value.code == "decision_idempotency_conflict"


def test_approval_revalidates_evidence_and_parent_active_revision(
    release_case: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, analysis = release_case
    proposal = _proposal(artifact_root, analysis)
    monkeypatch.setattr(
        release_control, "_load_active_revision", lambda _root: "a" * 64
    )

    with pytest.raises(RetrievalReleaseError) as caught:
        _approve(artifact_root, proposal)

    assert caught.value.code == "parent_active_revision_conflict"
    pointer_dir = artifact_root / "retrieval-release-decisions" / "by-proposal"
    assert not list(pointer_dir.glob("*.json"))


def test_reject_is_terminal_without_claiming_validation_or_active(
    release_case: tuple[Path, dict[str, Any]],
) -> None:
    artifact_root, analysis = release_case
    proposal = _proposal(artifact_root, analysis)

    decision = apply_retrieval_release_decision(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        decision="reject",
        client_action_id="owner-reject-001",
        actor_id="owner",
        revision_provider=lambda _root: "d" * 40,
    )

    assert decision["lifecycle"] == "rejected"
    assert decision["validation_required"] is False
    assert decision["activation_status"] == "not_active"
    with pytest.raises(RetrievalReleaseError) as caught:
        record_retrieval_release_outcome(
            project_root=ROOT,
            artifact_root=artifact_root,
            proposal_id=proposal["proposal_id"],
            proposal_revision=proposal["proposal_revision"],
            outcome="validation_failed",
            validation_receipt=build_retrieval_validation_failure_receipt(
                proposal,
                error_code="sentinel_failed",
            ),
        )
    assert caught.value.code == "release_not_approved"


def test_validation_failure_outcome_is_strict_and_idempotent(
    release_case: tuple[Path, dict[str, Any]],
) -> None:
    artifact_root, analysis = release_case
    proposal = _proposal(artifact_root, analysis)
    _approve(artifact_root, proposal)
    receipt = build_retrieval_validation_failure_receipt(
        proposal,
        error_code="sentinel_failed",
    )

    first = record_retrieval_release_outcome(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        outcome="validation_failed",
        validation_receipt=receipt,
    )
    second = record_retrieval_release_outcome(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        outcome="validation_failed",
        validation_receipt=receipt,
    )

    assert first == second
    assert first["lifecycle"] == "validation_failed"
    assert first["active_strategy_revision"] is None
    catalog = load_retrieval_release_catalog(
        project_root=ROOT,
        artifact_root=artifact_root,
    )
    assert catalog["releases"][0]["lifecycle"] == "validation_failed"
    assert catalog["active_retrieval_release"] is None


def test_active_outcome_requires_receipt_and_real_active_revision(
    release_case: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, analysis = release_case
    proposal = _proposal(artifact_root, analysis)
    _approve(artifact_root, proposal)
    active_revision = "a" * 64
    receipt = _activation_receipt(proposal, strategy_revision=active_revision)
    monkeypatch.setattr(
        release_control,
        "_load_active_revision",
        lambda _root: active_revision,
    )

    outcome = record_retrieval_release_outcome(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        outcome="active",
        validation_receipt=receipt,
        active_strategy_revision=active_revision,
    )

    assert outcome["lifecycle"] == "active"
    assert outcome["active_strategy_revision"] == active_revision
    catalog = load_retrieval_release_catalog(
        project_root=ROOT,
        artifact_root=artifact_root,
    )
    assert (
        catalog["active_retrieval_release"]["proposal_id"] == (proposal["proposal_id"])
    )
    active = catalog["active_retrieval_release"]
    assert active["ready"] is True
    assert active["health"] == "ready"
    assert active["strategy_revision"] == active_revision
    assert active["previous_revision"] == receipt["rollback_strategy_revision"]
    assert active["index_id"] == receipt["index_id"]


def test_rollback_is_idempotent_and_catalog_fails_closed_on_pointer_move(
    release_case: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, analysis = release_case
    proposal = _proposal(artifact_root, analysis)
    _approve(artifact_root, proposal)
    activated_revision = "a" * 64
    target_revision = "b" * 64
    pointer = {"revision": activated_revision}
    monkeypatch.setattr(
        release_control,
        "_load_active_revision",
        lambda _root: pointer["revision"],
    )
    record_retrieval_release_outcome(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        outcome="active",
        validation_receipt=_activation_receipt(
            proposal,
            strategy_revision=activated_revision,
        ),
        active_strategy_revision=activated_revision,
    )

    receipt = _rollback_receipt(
        from_strategy_revision=activated_revision,
        target_strategy_revision=target_revision,
    )
    with pytest.raises(RetrievalReleaseError) as stale_pointer:
        record_retrieval_release_rollback(
            project_root=ROOT,
            artifact_root=artifact_root,
            proposal_id=proposal["proposal_id"],
            proposal_revision=proposal["proposal_revision"],
            rollback_receipt=receipt,
        )
    assert stale_pointer.value.code == "rollback_pointer_conflict"

    pointer["revision"] = target_revision
    before_record = load_retrieval_release_catalog(
        project_root=ROOT,
        artifact_root=artifact_root,
    )
    assert before_record["releases"][0]["lifecycle"] == "rolled_back"
    assert before_record["releases"][0]["active_strategy_revision"] is None
    assert before_record["releases"][0]["rollback_id"] is None
    assert before_record["active_retrieval_release"] is None

    first = record_retrieval_release_rollback(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        rollback_receipt=receipt,
    )
    second = record_retrieval_release_rollback(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
        rollback_receipt=receipt,
    )

    assert first == second
    assert first["lifecycle"] == "rolled_back"
    assert first["from_strategy_revision"] == activated_revision
    assert first["target_strategy_revision"] == target_revision
    loaded = load_retrieval_release(
        project_root=ROOT,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        proposal_revision=proposal["proposal_revision"],
    )
    assert loaded["lifecycle"] == "rolled_back"
    assert loaded["rollback"] == first
    catalog = load_retrieval_release_catalog(
        project_root=ROOT,
        artifact_root=artifact_root,
    )
    assert catalog["active_retrieval_release"] is None
    assert catalog["releases"][0]["rollback_id"] == first["rollback_id"]
    assert catalog["releases"][0]["rollback_receipt_id"] == receipt["receipt_id"]
    assert catalog["releases"][0]["rollback_target_revision"] == target_revision


def test_release_logs_are_independent_and_privacy_safe(
    release_case: tuple[Path, dict[str, Any]],
) -> None:
    artifact_root, analysis = release_case
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"retrieval_release": "INFO"},
        stream=stream,
    )

    with logging_context(trace_id="release-request-safe"):
        proposal = _proposal(artifact_root, analysis)
        _approve(artifact_root, proposal)

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events
    assert {event["module"] for event in events} == {"retrieval_release"}
    assert all(event["trace_id"] == "release-request-safe" for event in events)
    assert {event["event"] for event in events} >= {
        "retrieval_release_proposal_started",
        "retrieval_release_proposal_stored",
        "retrieval_release_decision_recorded",
    }
    serialized = stream.getvalue().lower()
    query = analysis["comparison"]["per_query"][0]["query_text"].lower()
    product = analysis["comparison"]["per_query"][0]["candidate_top_results"][0]
    assert query not in serialized
    assert product["product_id"].lower() not in serialized
    assert product["product_title"].lower() not in serialized
    assert "owner-action-001" not in serialized
    assert '"actor_id"' not in serialized
