from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from search_quality.data.contracts import (
    DataContractError,
    DatasetLock,
    SourceSpec,
    Stage1Config,
    sha256_file,
    validate_source_files,
)
from search_quality.data.esci import (
    build_stage1,
    canonical_frame_sha256,
    prepare_stage1,
)


def fixture_config() -> Stage1Config:
    return Stage1Config(
        schema_version="fixture-v1",
        source_commit="fixture-commit",
        locale="us",
        dataset_version_column="small_version",
        dataset_version_value=1,
        official_train_value="train",
        official_test_value="test",
        dev_query_count=2,
        smoke_query_count=1,
        split_seed="fixture-seed",
        valid_labels=("E", "S", "C", "I"),
    )


def write_fixture(
    root: Path,
    *,
    invalid_label: bool = False,
    conflicting_label: bool = False,
    reverse_examples: bool = False,
) -> DatasetLock:
    rows = []
    example_id = 0
    for query_id in range(1, 7):
        origin_split = "train" if query_id <= 4 else "test"
        for product_number, label in ((1, "E"), (2, "I")):
            rows.append(
                {
                    "example_id": example_id,
                    "query": f"query {query_id}",
                    "query_id": query_id,
                    "product_id": f"p{query_id}-{product_number}",
                    "product_locale": "us",
                    "esci_label": label,
                    "small_version": 1,
                    "large_version": 1,
                    "split": origin_split,
                }
            )
            example_id += 1
    if invalid_label:
        rows[0]["esci_label"] = "X"
    if conflicting_label:
        conflict = dict(rows[0])
        conflict["example_id"] = 999
        conflict["esci_label"] = "S"
        rows.append(conflict)
    examples = pl.DataFrame(rows)
    if reverse_examples:
        examples = examples.reverse()

    products = []
    for query_id in range(1, 7):
        for product_number in (1, 2):
            products.append(
                {
                    "product_id": f"p{query_id}-{product_number}",
                    "product_title": f"Product {query_id}-{product_number}",
                    "product_description": "fixture description",
                    "product_bullet_point": "fixture bullet",
                    "product_brand": "Fixture",
                    "product_color": "Blue",
                    "product_locale": "us",
                }
            )
    products.append(
        {
            "product_id": "p1-1",
            "product_title": "Wrong locale product",
            "product_description": "",
            "product_bullet_point": "",
            "product_brand": "",
            "product_color": "",
            "product_locale": "es",
        }
    )
    sources = pl.DataFrame({"query_id": list(range(1, 7)), "source": ["other"] * 6})

    filenames = {
        "shopping_queries_dataset_examples.parquet": examples,
        "shopping_queries_dataset_products.parquet": pl.DataFrame(products),
    }
    for filename, frame in filenames.items():
        frame.write_parquet(root / filename)
    sources.write_csv(root / "shopping_queries_dataset_sources.csv")

    specs = []
    for filename in (*filenames, "shopping_queries_dataset_sources.csv"):
        path = root / filename
        specs.append(
            SourceSpec(
                path=f"shopping_queries_dataset/{filename}",
                size=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    return DatasetLock(
        repository="https://example.invalid/esci",
        commit="fixture-commit",
        license="Apache-2.0",
        files=tuple(specs),
    )


def test_pipeline_builds_disjoint_formal_splits_and_dev_smoke(
    tmp_path: Path,
) -> None:
    lock = write_fixture(tmp_path)
    validate_source_files(tmp_path, lock)
    prepared = prepare_stage1(source_dir=tmp_path, config=fixture_config(), lock=lock)

    split_queries = {
        name: set(frame.get_column("query_id").unique().to_list())
        for name, frame in {
            "train": prepared.train,
            "dev": prepared.dev,
            "test": prepared.test,
        }.items()
    }
    assert len(split_queries["train"]) == 2
    assert len(split_queries["dev"]) == 2
    assert split_queries["test"] == {5, 6}
    assert split_queries["train"].isdisjoint(split_queries["dev"])
    assert split_queries["train"].isdisjoint(split_queries["test"])
    assert split_queries["dev"].isdisjoint(split_queries["test"])
    assert prepared.smoke.get_column("query_id").n_unique() == 1
    assert set(prepared.smoke["query_id"].to_list()) <= split_queries["dev"]
    assert prepared.smoke.get_column("eval_split").unique().to_list() == ["dev"]
    titles = []
    for frame in (prepared.train, prepared.dev, prepared.test):
        titles.extend(frame["product_title"].to_list())
    assert "Wrong locale product" not in titles


def test_input_order_does_not_change_splits_or_content_hash(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = prepare_stage1(
        source_dir=first_dir,
        config=fixture_config(),
        lock=write_fixture(first_dir),
    )
    second = prepare_stage1(
        source_dir=second_dir,
        config=fixture_config(),
        lock=write_fixture(second_dir, reverse_examples=True),
    )
    for name in ("train", "dev", "test", "smoke"):
        assert canonical_frame_sha256(getattr(first, name)) == canonical_frame_sha256(
            getattr(second, name)
        )


def test_invalid_esci_label_fails_fast(tmp_path: Path) -> None:
    lock = write_fixture(tmp_path, invalid_label=True)
    with pytest.raises(DataContractError, match="invalid ESCI labels"):
        prepare_stage1(source_dir=tmp_path, config=fixture_config(), lock=lock)


def test_conflicting_pair_labels_are_not_silently_deduplicated(
    tmp_path: Path,
) -> None:
    lock = write_fixture(tmp_path, conflicting_label=True)
    with pytest.raises(DataContractError, match="conflicting ESCI labels"):
        prepare_stage1(source_dir=tmp_path, config=fixture_config(), lock=lock)


def test_config_and_lock_must_pin_the_same_source(tmp_path: Path) -> None:
    lock = write_fixture(tmp_path)
    config = replace(fixture_config(), source_commit="other")
    with pytest.raises(DataContractError, match="different source commits"):
        prepare_stage1(source_dir=tmp_path, config=config, lock=lock)


def test_build_writes_parquet_manifest_and_report(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    lock = write_fixture(source_dir)
    manifest_path = tmp_path / "manifest.json"
    report_path = tmp_path / "report.md"
    output_dir = tmp_path / "processed"

    manifest = build_stage1(
        source_dir=source_dir,
        output_dir=output_dir,
        config=fixture_config(),
        lock=lock,
        manifest_path=manifest_path,
        report_path=report_path,
        project_root=tmp_path,
    )

    assert manifest_path.is_file()
    assert report_path.is_file()
    assert (output_dir / "smoke.parquet").is_file()
    assert manifest["evaluation_boundary"]["smoke_is_formal_split"] is False
    assert "candidate-set reranking" in report_path.read_text(encoding="utf-8")
