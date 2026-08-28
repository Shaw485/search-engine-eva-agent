from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from search_quality.observability import configure_logging
from search_quality.query_constructor import builder
from search_quality.query_constructor import cli as query_constructor_cli
from search_quality.query_constructor.cli import build_parser
from search_quality.query_constructor.contracts import (
    QueryCase,
    QueryConstruction,
    QueryDropReason,
    QuerySetArtifact,
)
from search_quality.query_constructor.identity import query_set_id

ROOT = Path(__file__).resolve().parents[1]
TEST_REVISION = "a" * 40


def _build(*, project_root: Path = ROOT) -> QuerySetArtifact:
    return builder.build_smoke_query_set(
        project_root=project_root,
        revision_provider=lambda _root: TEST_REVISION,
    )


def test_source_access_recorder_observes_only_smoke() -> None:
    observed: list[str] = []
    artifact = builder.build_smoke_query_set(
        project_root=ROOT,
        revision_provider=lambda _root: TEST_REVISION,
        profile_access_recorder=observed.append,
    )

    assert artifact.query_count == 59
    assert observed == ["smoke"]


def _copy_pinned_metadata(project_root: Path) -> None:
    contract = project_root / builder.DEFAULT_SOURCE_CONTRACT
    contract.parent.mkdir(parents=True)
    shutil.copy2(ROOT / builder.DEFAULT_SOURCE_CONTRACT, contract)
    manifest = project_root / "data" / "manifests" / "esci-stage1.json"
    manifest.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "data" / "manifests" / "esci-stage1.json", manifest)


def test_query_constructor_is_deterministic_and_keeps_synthetic_queries_unjudged() -> (
    None
):
    first = _build()
    second = _build()

    assert first == second
    assert first.query_set_id == second.query_set_id
    assert first.query_count == 59
    assert first.original_count == 20
    assert first.synthetic_count == 39
    assert first.deduplicated_count == 0
    assert first.formal_evaluation_allowed is False
    assert first.locked_profiles_not_read == ("dev", "test")
    assert len({item.normalized_query_sha256 for item in first.cases}) == 59
    assert all(item.development_seen for item in first.cases)
    assert all(not item.eligible_for_final_evaluation for item in first.cases)
    assert all(not item.synthetic_labels_inherited for item in first.cases)
    original = [item for item in first.cases if item.construction == "identity"]
    synthetic = [item for item in first.cases if item.construction != "identity"]
    assert (
        sum(item.construction == "adjacent_transposition" for item in first.cases) == 20
    )
    assert (
        sum(item.construction == "token_order_reversal" for item in first.cases) == 19
    )
    assert all(item.label_scope == "smoke_judged_candidates" for item in original)
    assert all(item.label_scope == "unjudged" for item in synthetic)
    assert all(
        item.intended_use == "exploratory_bad_case_discovery" for item in synthetic
    )
    assert first.code_revision == TEST_REVISION
    assert first.source_id == "esci-stage1-smoke-v1"
    assert first.source_query_count == 20
    assert len(first.source_contract_sha256) == 64
    assert all(item.source.source_id == first.source_id for item in first.cases)
    assert all(
        item.source.source_contract_sha256 == first.source_contract_sha256
        for item in first.cases
    )


def test_clean_revision_gate_runs_before_contract_manifest_or_parquet_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_io = False

    def forbidden_io(*_args, **_kwargs):
        nonlocal reached_io
        reached_io = True
        raise AssertionError("Query source I/O was reached")

    def fail_revision(_root: Path) -> str:
        raise RuntimeError("dirty worktree")

    monkeypatch.setattr(builder, "_load_source_contract", forbidden_io)
    monkeypatch.setattr(builder, "sha256_file", forbidden_io)
    monkeypatch.setattr(builder.pl, "read_parquet", forbidden_io)

    with pytest.raises(RuntimeError, match="dirty worktree"):
        builder.build_smoke_query_set(
            project_root=ROOT,
            revision_provider=fail_revision,
        )
    assert reached_io is False


@pytest.mark.parametrize("profile", ["dev", "test", "train", "unknown"])
def test_protected_or_unknown_query_sources_fail_before_profile_loading(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = False
    revision_checked = False

    def forbidden_loader(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("protected source loader was reached")

    def forbidden_revision(_root: Path) -> str:
        nonlocal revision_checked
        revision_checked = True
        raise AssertionError("revision gate was reached")

    monkeypatch.setattr(
        builder.EvaluationProfile,
        "from_stage1_manifest",
        forbidden_loader,
    )
    with pytest.raises(ValueError, match="restricted to the smoke profile"):
        builder.build_smoke_query_set(
            project_root=ROOT / "protected-source-must-not-be-resolved",
            source_profile=profile,
            revision_provider=forbidden_revision,
        )
    assert opened is False
    assert revision_checked is False


def test_independent_manifest_pin_rejects_change_before_profile_or_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_pinned_metadata(tmp_path)
    manifest = tmp_path / "data" / "manifests" / "esci-stage1.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    profile_loaded = False
    source_read = False

    def forbidden_profile(*_args, **_kwargs):
        nonlocal profile_loaded
        profile_loaded = True
        raise AssertionError("profile parser was reached")

    def forbidden_read(*_args, **_kwargs):
        nonlocal source_read
        source_read = True
        raise AssertionError("Parquet reader was reached")

    monkeypatch.setattr(
        builder.EvaluationProfile,
        "from_stage1_manifest",
        forbidden_profile,
    )
    monkeypatch.setattr(builder.pl, "read_parquet", forbidden_read)

    with pytest.raises(ValueError, match="manifest does not match"):
        _build(project_root=tmp_path)
    assert profile_loaded is False
    assert source_read is False


def test_independent_source_pin_rejects_change_before_parquet_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_pinned_metadata(tmp_path)
    source = tmp_path / "data" / "samples" / "esci-stage1-smoke.parquet"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not-the-pinned-smoke-source")
    source_read = False

    def forbidden_read(*_args, **_kwargs):
        nonlocal source_read
        source_read = True
        raise AssertionError("Parquet reader was reached")

    monkeypatch.setattr(builder.pl, "read_parquet", forbidden_read)

    with pytest.raises(ValueError, match="independent pin"):
        _build(project_root=tmp_path)
    assert source_read is False


def test_intermediate_source_directory_symlink_is_rejected_before_hash_or_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_pinned_metadata(tmp_path)
    samples = tmp_path / "data" / "samples"
    samples.symlink_to(ROOT / "data" / "samples", target_is_directory=True)
    source_hashed = False
    source_read = False
    real_sha256_file = builder.sha256_file

    def recording_hash(path: Path) -> str:
        nonlocal source_hashed
        if Path(path).suffix == ".parquet":
            source_hashed = True
            raise AssertionError("source hash was reached")
        return real_sha256_file(path)

    def forbidden_read(*_args, **_kwargs):
        nonlocal source_read
        source_read = True
        raise AssertionError("Parquet reader was reached")

    monkeypatch.setattr(builder, "sha256_file", recording_hash)
    monkeypatch.setattr(builder.pl, "read_parquet", forbidden_read)

    with pytest.raises(ValueError, match="symbolic link"):
        _build(project_root=tmp_path)
    assert source_hashed is False
    assert source_read is False


def test_constructor_reads_only_query_provenance_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_columns = None
    real_reader = pl.read_parquet

    def recording_reader(*args, **kwargs):
        nonlocal observed_columns
        observed_columns = tuple(kwargs.get("columns", ()))
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(builder.pl, "read_parquet", recording_reader)
    artifact = _build()

    assert artifact.original_count == 20
    assert observed_columns == (
        "query_id",
        "query_text",
        "source",
        "product_locale",
        "eval_split",
        "origin_split",
        "is_smoke",
    )
    assert "esci_label" not in observed_columns
    assert not any(
        name.startswith("product_") and name != "product_locale"
        for name in observed_columns
    )


def test_query_constructor_contract_rejects_inherited_synthetic_labels() -> None:
    artifact = _build()
    synthetic = next(item for item in artifact.cases if item.construction != "identity")
    payload = synthetic.model_dump(mode="json")
    payload["synthetic_labels_inherited"] = True

    with pytest.raises(ValidationError, match="literal_error"):
        QueryCase.model_validate(payload)


def test_all_identities_are_retained_before_synthetic_collisions_are_dropped() -> None:
    contract, contract_sha256 = builder._load_source_contract(
        ROOT / builder.DEFAULT_SOURCE_CONTRACT
    )
    source_queries = pl.DataFrame(
        {
            "query_id": [1, 2],
            "query_text": ["red shoes", "shoes red"],
            "product_locale": ["us", "us"],
            "source": ["other", "other"],
        }
    )
    cases, dropped = builder._construct_cases(
        source_queries,
        provenance=builder._BuildProvenance(
            contract=contract,
            source_contract_sha256=contract_sha256,
            code_revision=TEST_REVISION,
        ),
    )

    identities = [
        item for item in cases if item.construction == QueryConstruction.IDENTITY
    ]
    assert [(item.source.query_id, item.query_text) for item in identities] == [
        (1, "red shoes"),
        (2, "shoes red"),
    ]
    reversal_drops = [
        item
        for item in dropped
        if item.construction == QueryConstruction.TOKEN_ORDER_REVERSAL
    ]
    assert len(reversal_drops) == 2
    assert {item.reason for item in reversal_drops} == {
        QueryDropReason.DUPLICATES_IDENTITY
    }
    identities_by_id = {item.case_id: item for item in identities}
    assert {
        identities_by_id[item.collides_with_case_id].query_text
        for item in reversal_drops
    } == {"red shoes", "shoes red"}


def test_nfkc_normalization_defines_query_identity() -> None:
    assert builder._normalize_query("  ＩPhone   PRO\tCase ") == "iphone pro case"
    assert builder._normalize_query("① wireless") == "1 wireless"


def test_artifact_validation_recomputes_content_ids_and_provenance() -> None:
    artifact = _build()
    payload = artifact.model_dump(mode="json")
    payload["source_contract_sha256"] = "b" * 64

    with pytest.raises(ValidationError):
        QuerySetArtifact.model_validate(payload)

    invalid = artifact.model_copy(update={"query_set_id": "query-set-000000000000"})
    with pytest.raises(ValidationError, match="Query set ID"):
        builder.validate_query_set(invalid)


def test_artifact_validation_rejects_an_incomplete_synthetic_construction() -> None:
    payload = _build().model_dump(mode="json")
    removed = next(
        item for item in payload["cases"] if item["construction"] != "identity"
    )
    payload["cases"].remove(removed)
    payload["query_count"] -= 1
    payload["synthetic_count"] -= 1
    payload["query_set_id"] = query_set_id(
        {key: value for key, value in payload.items() if key != "query_set_id"}
    )
    incomplete = QuerySetArtifact.model_validate(payload)

    with pytest.raises(ValueError, match="complete synthetic construction"):
        builder.validate_query_set(incomplete)


def test_store_validates_before_touching_artifact_root(tmp_path: Path) -> None:
    artifact = _build().model_copy(update={"query_set_id": "query-set-000000000000"})
    target = tmp_path / "must-not-be-created"

    with pytest.raises(ValidationError, match="Query set ID"):
        builder.store_query_set(artifact, artifact_root=target)
    assert not target.exists()


def test_query_constructor_store_rejects_symlink_root(tmp_path: Path) -> None:
    artifact = _build()
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        builder.store_query_set(artifact, artifact_root=linked)


def test_query_constructor_logs_counts_but_not_query_text() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"query_constructor": "INFO"},
        stream=stream,
    )
    artifact = _build()
    contents = stream.getvalue()
    events = [json.loads(line) for line in contents.splitlines() if line]

    assert events
    assert {item["module"] for item in events} == {"query_constructor"}
    assert artifact.cases[0].query_text not in contents
    assert '"query_text"' not in contents
    assert '"product_id"' not in contents
    assert '"path"' not in contents


def test_query_constructor_cli_has_no_source_or_input_override() -> None:
    destinations = {action.dest for action in build_parser()._actions}
    assert "source_profile" not in destinations
    assert "profile" not in destinations
    assert "input" not in destinations
    assert "artifact_root" not in destinations


def test_query_constructor_cli_failure_is_actionable_and_private(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args):
        raise ValueError("private Query and /private/source/path")

    monkeypatch.setattr(query_constructor_cli, "_execute", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "search-quality-query-constructor",
            "--log-level",
            "OFF",
            "--log-module",
            "query_constructor=INFO",
        ],
    )
    with pytest.raises(SystemExit) as captured:
        query_constructor_cli.main()
    diagnostics = capsys.readouterr().err
    configure_logging(default_level="OFF", stream=io.StringIO())

    assert captured.value.code == 1
    event = json.loads(diagnostics)
    assert event["module"] == "query_constructor"
    assert event["event"] == "query_constructor_command_failed"
    assert event["error_type"] == "ValueError"
    assert "private Query" not in diagnostics
    assert "/private/source/path" not in diagnostics
