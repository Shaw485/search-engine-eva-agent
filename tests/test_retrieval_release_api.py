from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.api import main as api

PROPOSAL_ID = "retrieval-proposal-aaaaaaaaaaaa"
PROPOSAL_REVISION = "a" * 64
ACTIVE_REVISION = "b" * 64
BASELINE_REVISION = "d" * 64


class _BaselineCatalogService:
    metadata = SimpleNamespace(
        index_id="catalog-baseline-v1-0123456789ab",
        product_count=1_814_924,
    )


class _ActiveResult:
    def to_dict(self) -> dict:
        return {
            "backend": "sqlite-fts5",
            "channel_counts": {
                "coarse": 10,
                "exact": 4,
                "fused": 12,
                "multi_field": 50,
                "title": 50,
                "union": 57,
            },
            "hits": [],
            "index_id": "catalog-active-v2-0123456789ab",
            "index_schema_version": "catalog-index-v2",
            "locale_counts": {"us": 1},
            "mode": "v2",
            "pipeline_id": "catalog-multifield-rrf-v2",
            "product_count": 1_814_924,
            "strategy_id": "catalog-multifield-rrf-v2",
            "strategy_revision": ACTIVE_REVISION,
        }


class _ActiveCatalogService:
    def __init__(self, *, ready: bool = True, mode: str = "v2") -> None:
        self._ready = ready
        self._mode = mode
        self.search_calls: list[tuple[str, int]] = []

    def readiness(self) -> dict:
        return {
            "index_id": "catalog-active-v2-0123456789ab",
            "mode": self._mode,
            "ready": self._ready,
            "strategy_revision": ACTIVE_REVISION,
        }

    def search(self, query: str, *, top_k: int) -> _ActiveResult:
        self.search_calls.append((query, top_k))
        return _ActiveResult()


def _request(*, approval_token: str | None = None) -> Request:
    headers = []
    if approval_token is not None:
        headers.append((b"x-search-approval-token", approval_token.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/retrieval/release/decision",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_health_exposes_active_serving_separately_from_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _ActiveCatalogService()
    monkeypatch.setattr(api, "get_catalog_search_service", _BaselineCatalogService)
    monkeypatch.setattr(api, "get_active_catalog_search_service", lambda: active)

    response = api.health()

    assert response["catalog"] == {
        "index_id": "catalog-baseline-v1-0123456789ab",
        "product_count": 1_814_924,
        "status": "ready",
    }
    assert response["active_serving"]["status"] == "active"
    assert response["active_serving"]["ready"] is True
    assert response["active_serving"]["strategy_revision"] == ACTIVE_REVISION


def test_active_search_rejects_an_unreleased_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "get_active_catalog_search_service",
        lambda: _ActiveCatalogService(ready=True, mode="baseline"),
    )

    with pytest.raises(HTTPException) as captured:
        api.catalog_search_active_post(
            api.CatalogSearchRequest(query="wireless mouse", top_k=10)
        )

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "active_strategy_unavailable"


def test_active_search_returns_the_actual_serving_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ActiveCatalogService()
    monkeypatch.setattr(api, "get_active_catalog_search_service", lambda: service)

    response = api.catalog_search_active_post(
        api.CatalogSearchRequest(query="wireless mouse", top_k=10)
    )

    assert service.search_calls == [("wireless mouse", 10)]
    assert response["execution"] == {
        "channels": response["channel_counts"],
        "index_id": "catalog-active-v2-0123456789ab",
        "lane": "active",
        "strategy_id": "catalog-multifield-rrf-v2",
        "strategy_revision": ACTIVE_REVISION,
    }
    assert response["mode"] == "v2"


def test_release_session_token_is_action_bound_and_single_use() -> None:
    with api._RETRIEVAL_RELEASE_SESSION_LOCK:
        api._RETRIEVAL_RELEASE_SESSIONS.clear()
    token, _expires_at = api._create_retrieval_release_session(
        actor_id="c" * 64,
        proposal_id=PROPOSAL_ID,
        proposal_revision=PROPOSAL_REVISION,
        action="decision",
    )

    try:
        with pytest.raises(HTTPException) as wrong_action:
            api._consume_retrieval_release_session(
                _request(approval_token=token),
                actor_id="c" * 64,
                action="rollback",
            )
        assert wrong_action.value.status_code == 403

        # A mismatched attempt consumes the capability, preventing replay with a
        # corrected action or payload.
        with pytest.raises(HTTPException) as replay:
            api._consume_retrieval_release_session(
                _request(approval_token=token),
                actor_id="c" * 64,
                action="decision",
                proposal_id=PROPOSAL_ID,
                proposal_revision=PROPOSAL_REVISION,
            )
        assert replay.value.detail["code"] == "approval_session_invalid"
    finally:
        with api._RETRIEVAL_RELEASE_SESSION_LOCK:
            api._RETRIEVAL_RELEASE_SESSIONS.clear()


def test_release_session_token_accepts_exact_binding_once() -> None:
    with api._RETRIEVAL_RELEASE_SESSION_LOCK:
        api._RETRIEVAL_RELEASE_SESSIONS.clear()
    token, _expires_at = api._create_retrieval_release_session(
        actor_id="c" * 64,
        proposal_id=PROPOSAL_ID,
        proposal_revision=PROPOSAL_REVISION,
        action="decision",
    )

    try:
        session = api._consume_retrieval_release_session(
            _request(approval_token=token),
            actor_id="c" * 64,
            action="decision",
            proposal_id=PROPOSAL_ID,
            proposal_revision=PROPOSAL_REVISION,
        )
        assert session["proposal_id"] == PROPOSAL_ID

        with pytest.raises(HTTPException) as replay:
            api._consume_retrieval_release_session(
                _request(approval_token=token),
                actor_id="c" * 64,
                action="decision",
                proposal_id=PROPOSAL_ID,
                proposal_revision=PROPOSAL_REVISION,
            )
        assert replay.value.status_code == 403
    finally:
        with api._RETRIEVAL_RELEASE_SESSION_LOCK:
            api._RETRIEVAL_RELEASE_SESSIONS.clear()


def _patch_owner_and_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api,
        "_human_oracle_actor_from_request",
        lambda _request: SimpleNamespace(principal_hmac_sha256="c" * 64),
    )
    monkeypatch.setattr(
        api,
        "_consume_retrieval_release_session",
        lambda *_args, **_kwargs: {
            "proposal_id": PROPOSAL_ID,
            "proposal_revision": PROPOSAL_REVISION,
        },
    )
    monkeypatch.setattr(api, "_clear_public_retrieval_analysis_cache", lambda: None)


def _decision(decision: str) -> api.RetrievalReleaseDecisionRequest:
    return api.RetrievalReleaseDecisionRequest(
        proposal_id=PROPOSAL_ID,
        proposal_revision=PROPOSAL_REVISION,
        decision=decision,
        client_action_id=f"test-{decision}",
    )


def test_retrieval_release_routes_are_exact_post_routes() -> None:
    routes = {
        route.path: route
        for route in api.app.routes
        if getattr(route, "path", "").startswith("/agent/retrieval/release/")
    }

    assert set(routes) == {
        "/agent/retrieval/release/decision",
        "/agent/retrieval/release/rollback",
        "/agent/retrieval/release/session",
    }
    assert all(route.methods == {"POST"} for route in routes.values())


def test_owner_reject_records_decision_without_activating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_owner_and_session(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        api,
        "apply_retrieval_release_decision",
        lambda **kwargs: calls.append(kwargs["decision"]),
    )
    monkeypatch.setattr(
        api,
        "validate_and_activate_retrieval_strategy",
        lambda *_args, **_kwargs: pytest.fail("reject must not activate"),
    )
    monkeypatch.setattr(
        api,
        "_retrieval_release_projection",
        lambda proposal_id: {"proposal_id": proposal_id, "lifecycle": "rejected"},
    )

    response = api.retrieval_release_decision(_decision("reject"), _request())

    assert calls == ["reject"]
    assert response["lifecycle"] == "rejected"


def test_owner_approve_validates_activates_and_records_active_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_owner_and_session(monkeypatch)
    outcomes: list[dict] = []
    receipt = {"strategy_revision": ACTIVE_REVISION}
    monkeypatch.setattr(api, "apply_retrieval_release_decision", lambda **_kw: None)
    monkeypatch.setattr(
        api,
        "load_retrieval_release",
        lambda **_kw: {"proposal": {"proposal_id": PROPOSAL_ID}},
    )
    monkeypatch.setattr(
        api,
        "load_retrieval_activation_envelope",
        lambda **_kw: {"proposal": {}, "decision": {}},
    )
    monkeypatch.setattr(
        api,
        "validate_and_activate_retrieval_strategy",
        lambda *_args: receipt,
    )
    monkeypatch.setattr(
        api,
        "record_retrieval_release_outcome",
        lambda **kwargs: outcomes.append(kwargs),
    )
    monkeypatch.setattr(
        api,
        "_retrieval_release_projection",
        lambda proposal_id: {"proposal_id": proposal_id, "lifecycle": "active"},
    )

    response = api.retrieval_release_decision(_decision("approve"), _request())

    assert response["lifecycle"] == "active"
    assert outcomes[0]["outcome"] == "active"
    assert outcomes[0]["validation_receipt"] is receipt
    assert outcomes[0]["active_strategy_revision"] == ACTIVE_REVISION


def test_owner_approve_records_validation_failed_instead_of_fake_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_owner_and_session(monkeypatch)
    outcomes: list[dict] = []
    proposal = {"proposal_id": PROPOSAL_ID}
    monkeypatch.setattr(api, "apply_retrieval_release_decision", lambda **_kw: None)
    monkeypatch.setattr(
        api, "load_retrieval_release", lambda **_kw: {"proposal": proposal}
    )
    monkeypatch.setattr(
        api,
        "load_retrieval_activation_envelope",
        lambda **_kw: {"proposal": {}, "decision": {}},
    )

    class _SentinelFailure(ValueError):
        error_code = "retrieval_activation_sentinel_failed"

    def fail_activation(*_args):
        raise _SentinelFailure("sentinel failed")

    monkeypatch.setattr(
        api, "validate_and_activate_retrieval_strategy", fail_activation
    )
    monkeypatch.setattr(
        api,
        "build_retrieval_validation_failure_receipt",
        lambda value, *, error_code: {
            "proposal": value,
            "error_code": error_code,
        },
    )
    monkeypatch.setattr(
        api,
        "record_retrieval_release_outcome",
        lambda **kwargs: outcomes.append(kwargs),
    )
    monkeypatch.setattr(
        api,
        "_retrieval_release_projection",
        lambda proposal_id: {
            "proposal_id": proposal_id,
            "lifecycle": "validation_failed",
        },
    )

    response = api.retrieval_release_decision(_decision("approve"), _request())

    assert response["lifecycle"] == "validation_failed"
    assert outcomes[0]["outcome"] == "validation_failed"
    assert outcomes[0]["active_strategy_revision"] is None
    assert outcomes[0]["validation_receipt"]["error_code"] == (
        "retrieval_activation_sentinel_failed"
    )


def test_strategy_catalog_merges_retrieval_release_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = {
        "proposal_id": PROPOSAL_ID,
        "proposal_revision": PROPOSAL_REVISION,
        "lifecycle": "active",
    }
    monkeypatch.setattr(
        api,
        "load_strategy_catalog",
        lambda **_kw: {"schema_version": "agent-strategy-catalog-v1", "strategies": []},
    )
    monkeypatch.setattr(
        api,
        "load_retrieval_release_catalog",
        lambda **_kw: {
            "schema_version": "retrieval-release-catalog-v1",
            "releases": [release],
            "active_retrieval_release": release,
        },
    )

    response = api.agent_strategy_catalog()

    assert response["retrieval_releases"] == [release]
    assert response["active_retrieval_release"] == release
    assert response["retrieval_release_schema_version"] == (
        "retrieval-release-catalog-v1"
    )


def test_owner_rollback_is_cas_protected_and_persists_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_owner_and_session(monkeypatch)
    recorded: list[dict] = []
    monkeypatch.setattr(
        api,
        "load_retrieval_release",
        lambda **_kw: {
            "lifecycle": "active",
            "outcome": {
                "active_strategy_revision": ACTIVE_REVISION,
                "validation_receipt": {
                    "rollback_strategy_revision": BASELINE_REVISION,
                },
            },
        },
    )
    monkeypatch.setattr(
        api,
        "rollback_retrieval_strategy",
        lambda **kwargs: {
            "strategy_revision": BASELINE_REVISION,
            "rolled_back_from_revision": kwargs["expected_active_revision"],
        },
    )
    monkeypatch.setattr(
        api,
        "record_retrieval_release_rollback",
        lambda **kwargs: recorded.append(kwargs),
    )
    monkeypatch.setattr(
        api,
        "_retrieval_release_projection",
        lambda proposal_id: {"proposal_id": proposal_id, "lifecycle": "rolled_back"},
    )

    response = api.retrieval_release_rollback(
        api.RetrievalReleaseRollbackRequest(
            expected_active_revision=ACTIVE_REVISION,
            target_revision=BASELINE_REVISION,
            client_action_id="rollback-1",
        ),
        _request(),
    )

    assert response["lifecycle"] == "rolled_back"
    assert recorded[0]["proposal_id"] == PROPOSAL_ID
    assert recorded[0]["rollback_receipt"]["strategy_revision"] == BASELINE_REVISION
