from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import polars as pl
import pytest

from search_quality.catalog import index_v2, serving
from search_quality.catalog.index import build_catalog_index
from search_quality.catalog.index_v2 import build_catalog_index_v2
from search_quality.catalog.pipeline_v2 import (
    PRODUCTION_PIPELINE_CONFIG,
    PRODUCTION_PIPELINE_CONFIG_SHA256,
    PRODUCTION_PIPELINE_ID,
    PRODUCTION_STRATEGY_ID,
    CatalogV2SearchPipeline,
)
from search_quality.catalog.serving import (
    ACTIVE_POINTER_SCHEMA_VERSION,
    ActiveCatalogSearchService,
    RetrievalActivationRejected,
    RetrievalServingConfigurationError,
    load_active_retrieval_revision,
    rollback_retrieval_strategy,
    validate_and_activate_retrieval_strategy,
)
from search_quality.observability import configure_logging, logging_context

REVISION = "a" * 40


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


def _build_contract(source: Path, *, product_count: int) -> dict:
    return {
        "expected_source_size": source.stat().st_size,
        "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expected_product_count": product_count,
        "code_revision": REVISION,
    }


@pytest.fixture
def product_source(tmp_path: Path) -> Path:
    source = tmp_path / "products.parquet"
    pl.DataFrame(
        {
            "product_id": ["P1", "P2", "P3", "P4", "P5", "P6"],
            "product_locale": ["us", "us", "us", "us", "es", "jp"],
            "product_title": [
                "Acme Wireless Mouse",
                "Wireless Keyboard",
                "Wired Mouse",
                "Precision Office Peripheral",
                "Ratón Inalámbrico Azul",
                "ワイヤレス マウス 静音",
            ],
            "product_brand": ["Acme", "KeyCo", "Acme", "Acme", "Marca", "Neko"],
            "product_bullet_point": [
                "Silent travel mouse",
                "Compact keys",
                "USB cable",
                "Silent wireless mouse for travel",
                "Ligero",
                "静音",
            ],
            "product_description": [
                "2.4 GHz ergonomic mouse",
                "Office keyboard",
                "Reliable pointer",
                "Ergonomic office accessory",
                "Ratón azul",
                "ワイヤレス機器",
            ],
            "product_color": ["Black", "Black", "Black", "Gray", "Azul", "White"],
        }
    ).write_parquet(source)
    return source


@pytest.fixture
def catalog_indexes(product_source: Path, tmp_path: Path) -> tuple[Path, Path]:
    source_size = product_source.stat().st_size
    source_sha256 = hashlib.sha256(product_source.read_bytes()).hexdigest()
    baseline = tmp_path / "catalog-v1.sqlite3"
    active = tmp_path / "catalog-v2.sqlite3"
    common = {
        "expected_source_size": source_size,
        "expected_source_sha256": source_sha256,
        "expected_product_count": 6,
        "code_revision": REVISION,
        "batch_size": 2,
    }
    build_catalog_index(product_source, baseline, **common)
    build_catalog_index_v2(product_source, active, **common)
    return baseline, active


def test_v2_builder_uses_bounded_record_batches_and_safe_progress_logs(
    product_source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_batch_sizes: list[int] = []
    original = index_v2._normalize_product_batch

    def observe_batch(batch: pl.DataFrame) -> pl.DataFrame:
        observed_batch_sizes.append(batch.height)
        return original(batch)

    monkeypatch.setattr(index_v2, "_normalize_product_batch", observe_batch)
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"catalog_index": "INFO"},
        stream=stream,
    )
    output = tmp_path / "catalog-v2.sqlite3"
    build_catalog_index_v2(
        product_source,
        output,
        batch_size=2,
        **_build_contract(product_source, product_count=6),
    )

    assert observed_batch_sizes == [2, 2, 2]
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "catalog_v2_index_build_started",
        "catalog_v2_index_progress",
        "catalog_v2_index_build_completed",
    ]
    assert events[1]["rows_indexed"] == 2
    assert events[1]["batch_count"] == 2
    assert "Wireless Mouse" not in stream.getvalue()
    assert str(product_source) not in stream.getvalue()


def test_v2_builder_failure_keeps_existing_output_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicate-products.parquet"
    pl.DataFrame(
        {
            "product_id": ["P1", "P1", "P2"],
            "product_locale": ["us", "us", "us"],
            "product_title": ["First", "Duplicate", "Second"],
        }
    ).write_parquet(source, row_group_size=3)
    output = tmp_path / "catalog-v2.sqlite3"
    original = b"existing-index-remains-unchanged"
    output.write_bytes(original)

    with pytest.raises(sqlite3.IntegrityError):
        build_catalog_index_v2(
            source,
            output,
            batch_size=1,
            **_build_contract(source, product_count=3),
        )

    assert output.read_bytes() == original
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []


def test_external_content_integrity_check_detects_content_index_drift(
    product_source: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "catalog-v2.sqlite3"
    build_catalog_index_v2(
        product_source,
        output,
        batch_size=2,
        **_build_contract(product_source, product_count=6),
    )

    with sqlite3.connect(output) as connection:
        connection.execute(
            "UPDATE catalog_product_records SET title = ? WHERE rowid = 1",
            ("tampered external content",),
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "INSERT INTO catalog_products(catalog_products, rank) "
                "VALUES ('integrity-check', 1)"
            )


def _activation_envelope(*, parent_revision: str | None = None) -> dict:
    proposal_body = {
        "analysis_schema_version": "retrieval-stage-analysis-response-v1",
        "analysis_status": "proposal_ready",
        "approval_eligible": True,
        "code_revision": REVISION,
        "evidence": {"fixture": "bounded"},
        "lifecycle": "pending_owner_review",
        "parent_active_revision": parent_revision,
        "profile": "smoke",
        "release_gate": {"passed": True},
        "schema_version": "retrieval-release-proposal-v1",
        "selected_pipeline": {
            "config": json.loads(json.dumps(PRODUCTION_PIPELINE_CONFIG)),
            "config_sha256": PRODUCTION_PIPELINE_CONFIG_SHA256,
            "pipeline_id": PRODUCTION_PIPELINE_ID,
            "strategy_id": PRODUCTION_STRATEGY_ID,
        },
        "trace_terminal_reason_code": "candidate_passed_all_gates",
    }
    proposal_revision = _sha256_payload(proposal_body)
    proposal = {
        **proposal_body,
        "proposal_id": f"retrieval-proposal-{proposal_revision[:12]}",
        "proposal_revision": proposal_revision,
    }
    decision_body = {
        "activation_status": "not_active",
        "actor_id": "owner@example",
        "client_action_id": "fixture-action-1",
        "config_sha256": PRODUCTION_PIPELINE_CONFIG_SHA256,
        "decision": "approve",
        "lifecycle": "approved_for_validation",
        "parent_active_revision": parent_revision,
        "pipeline_id": PRODUCTION_PIPELINE_ID,
        "proposal_id": proposal["proposal_id"],
        "proposal_revision": proposal["proposal_revision"],
        "schema_version": "retrieval-release-decision-v1",
        "strategy_id": PRODUCTION_STRATEGY_ID,
        "validation_required": True,
    }
    decision = {
        **decision_body,
        "decision_id": ("retrieval-decision-" + _sha256_payload(decision_body)[:12]),
    }
    return {"decision": decision, "proposal": proposal}


def test_v2_index_and_three_channel_pipeline_use_full_product_fields(
    catalog_indexes: tuple[Path, Path],
) -> None:
    _baseline, active = catalog_indexes
    pipeline = CatalogV2SearchPipeline(active)

    result = pipeline.search("silent mouse")

    assert result.pipeline_id == PRODUCTION_PIPELINE_ID
    assert result.channel_counts == {
        "title": 2,
        "exact": 0,
        "multi_field": 3,
        "union": 3,
        "fused": 3,
        "coarse": 3,
    }
    hits = {hit.product.product_id: hit for hit in result.hits}
    assert "P4" in hits
    assert hits["P4"].product.bullet_point == "Silent wireless mouse for travel"
    assert pipeline.search("P4").hits[0].product.product_id == "P4"


def test_activation_switches_actual_strategy_and_rollback_is_atomic(
    catalog_indexes: tuple[Path, Path], tmp_path: Path
) -> None:
    baseline, active = catalog_indexes
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    service = ActiveCatalogSearchService(
        baseline_index_path=baseline,
        active_index_path=active,
        artifact_root=artifacts,
    )

    before = service.search("wireless mouse")
    assert before.mode == "baseline"
    assert before.strategy_revision is None
    assert before.channel_counts == {"baseline": 1}

    receipt = validate_and_activate_retrieval_strategy(
        _activation_envelope(),
        baseline,
        active,
        artifacts,
        lambda _root: REVISION,
    )

    assert receipt["passed"] is True
    assert receipt["active"] is True
    assert receipt["previous_strategy_revision"] is None
    assert receipt["parent_active_revision"] is None
    assert receipt["rollback_strategy_revision"] != receipt["strategy_revision"]
    assert set(receipt["channel_counts"]) == {
        "title",
        "exact",
        "multi_field",
        "union",
        "fused",
        "coarse",
    }
    pointer_path = artifacts / "retrieval-strategies" / "active.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer == {
        "schema_version": ACTIVE_POINTER_SCHEMA_VERSION,
        "strategy_id": PRODUCTION_STRATEGY_ID,
        "strategy_revision": receipt["strategy_revision"],
    }
    assert load_active_retrieval_revision(artifacts) == receipt["strategy_revision"]

    activated = service.search("silent mouse")
    assert activated.mode == "v2"
    assert activated.strategy_id == PRODUCTION_STRATEGY_ID
    assert activated.strategy_revision == receipt["strategy_revision"]
    assert activated.index_id == receipt["index_id"]
    assert set(activated.channel_counts) == set(receipt["channel_counts"])

    rollback = rollback_retrieval_strategy(
        baseline_index_path=baseline,
        active_index_path=active,
        artifact_root=artifacts,
        expected_active_revision=receipt["strategy_revision"],
    )
    assert rollback["mode"] == "baseline"
    assert rollback["strategy_revision"] == receipt["rollback_strategy_revision"]
    restored = service.search("wireless mouse")
    assert restored.mode == "baseline"
    assert restored.strategy_revision == rollback["strategy_revision"]


def test_invalid_config_and_index_identity_fail_closed_without_pointer_change(
    catalog_indexes: tuple[Path, Path], tmp_path: Path
) -> None:
    baseline, active = catalog_indexes
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    envelope = _activation_envelope()
    envelope["proposal"]["selected_pipeline"]["config"]["fusion"]["weights"][
        "multi-field-bm25-recall-v1"
    ] = 1.0

    with pytest.raises((ValueError, RetrievalActivationRejected)):
        validate_and_activate_retrieval_strategy(
            envelope,
            baseline,
            active,
            artifacts,
            lambda _root: REVISION,
        )
    assert not (artifacts / "retrieval-strategies" / "active.json").exists()

    receipt = validate_and_activate_retrieval_strategy(
        _activation_envelope(),
        baseline,
        active,
        artifacts,
        lambda _root: REVISION,
    )
    other_source = tmp_path / "other.parquet"
    pl.DataFrame(
        {
            "product_id": ["OTHER"],
            "product_locale": ["us"],
            "product_title": ["Other Product"],
        }
    ).write_parquet(other_source)
    other_index = tmp_path / "other.sqlite3"
    build_catalog_index_v2(
        other_source,
        other_index,
        expected_source_size=other_source.stat().st_size,
        expected_source_sha256=hashlib.sha256(other_source.read_bytes()).hexdigest(),
        expected_product_count=1,
        code_revision=REVISION,
    )
    wrong_service = ActiveCatalogSearchService(
        baseline_index_path=baseline,
        active_index_path=other_index,
        artifact_root=artifacts,
    )
    assert wrong_service.readiness()["ready"] is False
    with pytest.raises(RetrievalServingConfigurationError):
        wrong_service.search("wireless")
    assert load_active_retrieval_revision(artifacts) == receipt["strategy_revision"]


def test_receipt_publication_failure_never_advances_active_pointer(
    catalog_indexes: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, active = catalog_indexes
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    original = serving.write_immutable_json

    def fail_receipt(path: Path, payload: dict) -> None:
        if "activation-receipts" in path.parts:
            raise OSError("simulated receipt storage failure")
        original(path, payload)

    monkeypatch.setattr(serving, "write_immutable_json", fail_receipt)
    with pytest.raises(OSError, match="receipt storage failure"):
        validate_and_activate_retrieval_strategy(
            _activation_envelope(),
            baseline,
            active,
            artifacts,
            lambda _root: REVISION,
        )

    assert load_active_retrieval_revision(artifacts) is None
    assert not (artifacts / "retrieval-strategies" / "active.json").exists()


def test_serving_logs_are_module_isolated_and_redacted(
    catalog_indexes: tuple[Path, Path], tmp_path: Path
) -> None:
    baseline, active = catalog_indexes
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"catalog_serving": "DEBUG"},
        stream=stream,
    )
    service = ActiveCatalogSearchService(
        baseline_index_path=baseline,
        active_index_path=active,
        artifact_root=artifacts,
    )
    with logging_context(trace_id="serving-test"):
        service.search("Acme Wireless Mouse")

    contents = stream.getvalue()
    assert "Acme Wireless Mouse" not in contents
    assert "P1" not in contents
    events = [json.loads(line) for line in contents.splitlines()]
    assert {event["module"] for event in events} == {"catalog_serving"}
    assert events[0]["trace_id"] == "serving-test"
