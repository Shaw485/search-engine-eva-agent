from __future__ import annotations

import hashlib
import hmac
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.api import main as api
from search_quality.human_oracle import (
    BehaviorJudgment,
    BehaviorReason,
    IntentJudgment,
    IntentReason,
    OracleActor,
    OracleInvalidDecision,
)
from search_quality.observability import configure_logging, logging_context

ORIGIN = "https://shawspace.cn"
PRINCIPAL = "private-owner-name"
HMAC_KEY = "private-human-oracle-key-material-32-bytes-minimum"


def _request(
    *,
    origin: str | None = ORIGIN,
    content_type: str = "application/json",
    principal: str = PRINCIPAL,
):
    headers = [
        (b"content-type", content_type.encode()),
        (b"sec-fetch-site", b"same-origin"),
        (b"x-search-owner-principal", principal.encode()),
    ]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/human-oracle/batches/status",
            "headers": headers,
            "client": ("127.0.0.1", 41000),
        }
    )


@pytest.fixture
def oracle_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_HUMAN_ORACLE_ALLOWED_ORIGIN", ORIGIN)
    monkeypatch.setenv("SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY", HMAC_KEY)
    monkeypatch.setenv("SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY_ID", "owner-key-v1")
    monkeypatch.setenv(
        "SEARCH_HUMAN_ORACLE_OWNER_HMAC_SHA256",
        hmac.new(HMAC_KEY.encode(), PRINCIPAL.encode(), hashlib.sha256).hexdigest(),
    )


def test_owner_actor_is_server_derived_and_never_logged(
    oracle_environment: None,
) -> None:
    stream = io.StringIO()
    configure_logging(default_level="OFF", module_levels={"api": "INFO"}, stream=stream)

    actor = api._human_oracle_actor_from_request(_request())

    assert actor == OracleActor(
        principal_hmac_sha256=hmac.new(
            HMAC_KEY.encode(),
            PRINCIPAL.encode(),
            hashlib.sha256,
        ).hexdigest(),
        actor_key_id="owner-key-v1",
    )
    assert PRINCIPAL not in stream.getvalue()
    assert HMAC_KEY not in stream.getvalue()
    assert actor.principal_hmac_sha256 not in stream.getvalue()


@pytest.mark.parametrize(
    ("http_request", "expected_status"),
    [
        (_request(origin=None), 422),
        (_request(origin="https://attacker.example"), 422),
        (_request(content_type="text/plain"), 422),
    ],
)
def test_owner_request_requires_json_and_exact_same_origin(
    http_request: Request,
    expected_status: int,
    oracle_environment: None,
) -> None:
    with pytest.raises(HTTPException) as captured:
        api._human_oracle_actor_from_request(http_request)
    assert captured.value.status_code == expected_status
    assert captured.value.detail["code"] == "human_oracle_request_invalid"


def test_owner_actor_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEARCH_HUMAN_ORACLE_ALLOWED_ORIGIN", raising=False)
    monkeypatch.delenv("SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY", raising=False)
    monkeypatch.delenv("SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY_ID", raising=False)
    monkeypatch.delenv("SEARCH_HUMAN_ORACLE_OWNER_HMAC_SHA256", raising=False)
    with pytest.raises(HTTPException) as captured:
        api._human_oracle_actor_from_request(_request())
    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "human_oracle_unavailable"


def test_only_the_configured_owner_can_access_or_claim_batches(
    oracle_environment: None,
) -> None:
    stream = io.StringIO()
    configure_logging(default_level="OFF", module_levels={"api": "INFO"}, stream=stream)
    other_principal = "another-valid-basic-auth-user"
    with pytest.raises(HTTPException) as captured:
        api._human_oracle_actor_from_request(_request(principal=other_principal))
    assert captured.value.status_code == 403
    assert captured.value.detail == {
        "code": "human_oracle_owner_forbidden",
        "message": "Human Oracle access forbidden",
        "trace_id": captured.value.detail["trace_id"],
    }
    other_hmac = hmac.new(
        HMAC_KEY.encode(), other_principal.encode(), hashlib.sha256
    ).hexdigest()
    assert other_principal not in stream.getvalue()
    assert other_hmac not in stream.getvalue()
    assert HMAC_KEY not in stream.getvalue()


def test_human_oracle_routes_are_exact_post_with_strict_response_models() -> None:
    expected = {
        "/agent/human-oracle/batches/create": api.HumanOracleBatchCreateResponse,
        "/agent/human-oracle/batches/status": api.OracleReviewState,
        "/agent/human-oracle/intents/view": api.OracleIntentView,
        "/agent/human-oracle/intents/submit": api.HumanOracleIntentSubmitResponse,
        "/agent/human-oracle/behaviors/view": api.OracleBehaviorView,
        "/agent/human-oracle/behaviors/submit": (api.HumanOracleBehaviorSubmitResponse),
        "/agent/human-oracle/batches/seal": api.HumanOracleSealResponse,
    }
    observed = {
        route.path: route
        for route in api.app.routes
        if getattr(route, "path", "").startswith("/agent/human-oracle/")
    }
    assert set(observed) == set(expected)
    for path, response_model in expected.items():
        assert observed[path].methods == {"POST"}
        assert observed[path].response_model is response_model
        assert response_model.model_config["extra"] == "forbid"


def test_submission_requests_forbid_actor_results_and_invalid_reason_pairs() -> None:
    base = {
        "oracle_batch_id": "oracle-batch-aaaaaaaaaaaa",
        "unit_id": "oracle-unit-bbbbbbbbbbbb",
        "case_id": "query-case-cccccccccccc",
        "presentation_context_sha256": "d" * 64,
        "judgment": IntentJudgment.EQUIVALENT,
        "reason_code": IntentReason.SAME_PRODUCT_INTENT,
        "client_action_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "expected_previous_annotation_id": None,
    }
    with pytest.raises(ValueError):
        api.HumanOracleIntentSubmitRequest(
            **base,
            actor={"principal_hmac_sha256": "e" * 64},
        )
    with pytest.raises(ValueError):
        api.HumanOracleIntentSubmitRequest(**base, result_hits=[])
    with pytest.raises(ValueError, match="reason"):
        api.HumanOracleIntentSubmitRequest(
            **{**base, "reason_code": IntentReason.MEANING_CHANGED}
        )


@pytest.mark.parametrize(
    ("path", "judgment", "reason_code", "extra_fields"),
    [
        (
            "/agent/human-oracle/intents/submit",
            "equivalent",
            "same_product_intent",
            {},
        ),
        (
            "/agent/human-oracle/behaviors/submit",
            "uncertain",
            "catalog_coverage_unknown",
            {"intent_annotation_id": None},
        ),
    ],
)
def test_fastapi_body_boundary_accepts_legal_json_enum_strings(
    path: str,
    judgment: str,
    reason_code: str,
    extra_fields: dict[str, object],
) -> None:
    route = next(item for item in api.app.routes if getattr(item, "path", None) == path)
    raw_json_body = {
        "oracle_batch_id": "oracle-batch-aaaaaaaaaaaa",
        "unit_id": "oracle-unit-bbbbbbbbbbbb",
        "case_id": "query-case-cccccccccccc",
        "presentation_context_sha256": "d" * 64,
        "judgment": judgment,
        "reason_code": reason_code,
        "client_action_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "expected_previous_annotation_id": None,
        **extra_fields,
    }

    value, errors = route.body_field.validate(raw_json_body, {}, loc=("body",))
    assert errors == []
    assert value.judgment == judgment
    assert value.reason_code == reason_code

    invalid_value, invalid_errors = route.body_field.validate(
        {**raw_json_body, "judgment": "not-a-valid-judgment"},
        {},
        loc=("body",),
    )
    assert invalid_value is None
    assert invalid_errors

    extra_value, extra_errors = route.body_field.validate(
        {**raw_json_body, "actor": {"principal_hmac_sha256": "e" * 64}},
        {},
        loc=("body",),
    )
    assert extra_value is None
    assert extra_errors


def test_batch_create_uses_only_fixed_ids_and_returns_no_raw_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = []
    for index in range(20):
        candidate_count = 3 if index < 10 else 1
        units.append(
            SimpleNamespace(
                unit_id=f"oracle-unit-{index:012x}",
                source_case_id=f"query-case-{index:012x}",
                stratum=SimpleNamespace(
                    value=(
                        "source_zero_cluster"
                        if index < 10
                        else "source_nonzero_variant_zero"
                    )
                ),
                candidates=[object()] * candidate_count,
            )
        )
    batch = SimpleNamespace(
        oracle_batch_id="oracle-batch-aaaaaaaaaaaa",
        diagnostic_id="bad-case-bbbbbbbbbbbb",
        query_set_id="query-set-cccccccccccc",
        selected_cluster_count=20,
        selected_candidate_count=40,
        synthetic_intent_candidate_count=30,
        units=units,
        formal_evaluation_allowed=False,
        quality_conclusion_allowed=False,
        strategy_write_count=0,
    )
    diagnostic = object()
    query_set = object()
    repository = SimpleNamespace(
        create_batch=lambda value, **_kwargs: value,
    )
    monkeypatch.setattr(
        api,
        "_human_oracle_actor_from_request",
        lambda _request: OracleActor(
            principal_hmac_sha256="e" * 64,
            actor_key_id="owner-key-v1",
        ),
    )
    monkeypatch.setattr(api, "_runtime_artifact_root", lambda: "/trusted/runtime")

    def load(**kwargs):
        assert kwargs == {
            "artifact_root": "/trusted/runtime",
            "diagnostic_id": "bad-case-bbbbbbbbbbbb",
            "query_set_id": "query-set-cccccccccccc",
        }
        return diagnostic, query_set

    monkeypatch.setattr(api, "load_diagnostic_artifacts", load)
    monkeypatch.setattr(api, "build_oracle_batch", lambda **_kwargs: batch)
    monkeypatch.setattr(api, "_human_oracle_repository", lambda: repository)

    response = api.human_oracle_batch_create(
        _request(),
        api.HumanOracleBatchCreateRequest(
            diagnostic_id="bad-case-bbbbbbbbbbbb",
            query_set_id="query-set-cccccccccccc",
        ),
    )
    validated = api.HumanOracleBatchCreateResponse.model_validate(response, strict=True)
    assert validated.selected_candidate_count == 40
    assert validated.synthetic_intent_candidate_count == 30
    assert "query_text" not in response
    assert "products" not in response
    assert "actor" not in response


def test_behavior_view_requires_all_intents_before_catalog_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        review_state=lambda _batch_id: SimpleNamespace(
            projection=SimpleNamespace(active_intent_annotation_count=29)
        )
    )
    batch = SimpleNamespace(oracle_batch_id="oracle-batch-aaaaaaaaaaaa")
    monkeypatch.setattr(api, "_human_oracle_actor_from_request", lambda _request: None)
    monkeypatch.setattr(api, "_human_oracle_repository", lambda: repository)
    monkeypatch.setattr(
        api,
        "_load_human_oracle_evidence",
        lambda _repository, _batch_id: (batch, object(), object()),
    )
    replayed = False

    def replay(**_kwargs):
        nonlocal replayed
        replayed = True

    monkeypatch.setattr(api, "collect_behavior_samples_for_unit", replay)
    with logging_context(trace_id="oracle-intent-gate"):
        with pytest.raises(HTTPException) as captured:
            api.human_oracle_behavior_view(
                _request(),
                api.HumanOracleUnitRequest(
                    oracle_batch_id="oracle-batch-aaaaaaaaaaaa",
                    unit_id="oracle-unit-bbbbbbbbbbbb",
                ),
            )
    assert captured.value.status_code == 409
    assert replayed is False


def test_behavior_catalog_startup_failure_is_503_and_stale_evidence_is_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "get_catalog_search_service",
        lambda: (_ for _ in ()).throw(RuntimeError("private index path")),
    )
    with logging_context(trace_id="oracle-catalog-down"):
        with pytest.raises(HTTPException) as captured:
            api._get_human_oracle_catalog_service()
    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "human_oracle_unavailable"

    unit = SimpleNamespace(unit_id="oracle-unit-bbbbbbbbbbbb", candidates=[])
    batch = SimpleNamespace(
        oracle_batch_id="oracle-batch-aaaaaaaaaaaa",
        units=[unit],
    )
    repository = SimpleNamespace(
        review_state=lambda _batch_id: SimpleNamespace(
            projection=SimpleNamespace(active_intent_annotation_count=30)
        )
    )
    monkeypatch.setattr(api, "_human_oracle_actor_from_request", lambda _request: None)
    monkeypatch.setattr(api, "_human_oracle_repository", lambda: repository)
    monkeypatch.setattr(
        api,
        "_load_human_oracle_evidence",
        lambda _repository, _batch_id: (batch, object(), object()),
    )
    monkeypatch.setattr(
        api,
        "_get_human_oracle_catalog_service",
        lambda: object(),
    )
    monkeypatch.setattr(
        api,
        "collect_behavior_samples_for_unit",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private Query and changed result")
        ),
    )
    with logging_context(trace_id="oracle-evidence-stale"):
        with pytest.raises(HTTPException) as captured:
            api.human_oracle_behavior_view(
                _request(),
                api.HumanOracleUnitRequest(
                    oracle_batch_id="oracle-batch-aaaaaaaaaaaa",
                    unit_id="oracle-unit-bbbbbbbbbbbb",
                ),
            )
    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "human_oracle_state_conflict"
    assert "private Query" not in str(captured.value.detail)


def test_invalid_human_decision_maps_to_safe_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(submission):
        assert isinstance(submission.judgment, IntentJudgment)
        assert isinstance(submission.reason_code, IntentReason)
        raise OracleInvalidDecision("private judgment detail")

    repository = SimpleNamespace(submit_intent=reject)
    monkeypatch.setattr(
        api,
        "_human_oracle_actor_from_request",
        lambda _request: OracleActor(
            principal_hmac_sha256="e" * 64,
            actor_key_id="owner-key-v1",
        ),
    )
    monkeypatch.setattr(api, "_human_oracle_repository", lambda: repository)
    monkeypatch.setattr(
        api,
        "_load_human_oracle_evidence",
        lambda _repository, _batch_id: (object(), object(), object()),
    )
    payload = api.HumanOracleIntentSubmitRequest(
        oracle_batch_id="oracle-batch-aaaaaaaaaaaa",
        unit_id="oracle-unit-bbbbbbbbbbbb",
        case_id="query-case-cccccccccccc",
        presentation_context_sha256="d" * 64,
        judgment="equivalent",
        reason_code="same_product_intent",
        client_action_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    with logging_context(trace_id="oracle-invalid-decision"):
        with pytest.raises(HTTPException) as captured:
            api.human_oracle_intent_submit(_request(), payload)
    assert captured.value.status_code == 422
    assert captured.value.detail == {
        "code": "human_oracle_decision_invalid",
        "message": "Human Oracle judgment is invalid",
        "trace_id": "oracle-invalid-decision",
    }
    assert "private judgment detail" not in str(captured.value.detail)


def test_behavior_json_strings_are_converted_to_strict_core_enums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(submission):
        assert isinstance(submission.judgment, BehaviorJudgment)
        assert isinstance(submission.reason_code, BehaviorReason)
        raise OracleInvalidDecision("private behavior detail")

    repository = SimpleNamespace(
        review_state=lambda _batch_id: SimpleNamespace(
            projection=SimpleNamespace(active_intent_annotation_count=30)
        ),
        submit_behavior=reject,
    )
    monkeypatch.setattr(
        api,
        "_human_oracle_actor_from_request",
        lambda _request: OracleActor(
            principal_hmac_sha256="e" * 64,
            actor_key_id="owner-key-v1",
        ),
    )
    monkeypatch.setattr(api, "_human_oracle_repository", lambda: repository)
    monkeypatch.setattr(
        api,
        "_load_human_oracle_evidence",
        lambda _repository, _batch_id: (object(), object(), object()),
    )
    payload = api.HumanOracleBehaviorSubmitRequest(
        oracle_batch_id="oracle-batch-aaaaaaaaaaaa",
        unit_id="oracle-unit-bbbbbbbbbbbb",
        case_id="query-case-cccccccccccc",
        presentation_context_sha256="d" * 64,
        judgment="uncertain",
        reason_code="catalog_coverage_unknown",
        intent_annotation_id=None,
        client_action_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    with logging_context(trace_id="oracle-behavior-enum-conversion"):
        with pytest.raises(HTTPException) as captured:
            api.human_oracle_behavior_submit(_request(), payload)
    assert captured.value.status_code == 422
    assert captured.value.detail["code"] == "human_oracle_decision_invalid"
    assert "private behavior detail" not in str(captured.value.detail)
