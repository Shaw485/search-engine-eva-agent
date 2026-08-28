from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import polars as pl
import pytest

from search_quality.catalog.index import build_catalog_index
from search_quality.catalog.search import (
    CatalogBatchSearchFailed,
    CatalogSearchDeadlineExceeded,
    CatalogSearchService,
    InvalidCatalogQuery,
)
from search_quality.observability import configure_logging

REVISION = "a" * 40


@pytest.fixture
def catalog_source(tmp_path: Path) -> Path:
    source = tmp_path / "products.parquet"
    pl.DataFrame(
        {
            "product_id": [
                "B000EXACT1",
                "B000COMBO2",
                "B000WIRED3",
                "B000SPAN4",
                "B000JAPAN5",
                "B000PHONE6",
            ],
            "product_locale": ["us", "us", "us", "es", "jp", "us"],
            "product_title": [
                "Acme Wireless Mouse",
                "Wireless Keyboard and Mouse Combo",
                "Acme Wired Mouse",
                "Ratón Inalámbrico Azul",
                "ワイヤレス マウス 静音",
                "Protective Phone Case",
            ],
            "product_description": [None] * 6,
            "product_bullet_point": [None] * 6,
            "product_brand": ["Acme", "KeyCo", "Acme", None, "Neko", "CaseCo"],
            "product_color": ["Black", "Black", "Black", "Azul", None, "Blue"],
        }
    ).write_parquet(source)
    return source


@pytest.fixture
def catalog_index(catalog_source: Path, tmp_path: Path) -> Path:
    output = tmp_path / "catalog.sqlite3"
    _build(catalog_source, output)
    return output


def _identity(path: Path) -> tuple[int, str]:
    return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def _build(source: Path, output: Path, *, expected_count: int = 6):
    source_size, source_sha256 = _identity(source)
    return build_catalog_index(
        source,
        output,
        expected_source_size=source_size,
        expected_source_sha256=source_sha256,
        expected_product_count=expected_count,
        code_revision=REVISION,
        batch_size=2,
    )


def test_build_and_search_catalog_baseline(
    catalog_index: Path,
) -> None:
    service = CatalogSearchService(catalog_index)
    result = service.search("wireless mouse", top_k=10)

    assert result.product_count == 6
    assert result.locale_counts == {"es": 1, "jp": 1, "us": 4}
    assert [hit.product.product_id for hit in result.hits] == [
        "B000EXACT1",
        "B000COMBO2",
    ]
    assert [hit.rank for hit in result.hits] == [1, 2]
    assert all(hit.score >= 0 for hit in result.hits)
    assert all(hit.strategy == "sqlite-fts5-bm25" for hit in result.hits)


def test_exact_product_id_and_all_locales_are_searchable(
    catalog_index: Path,
) -> None:
    service = CatalogSearchService(catalog_index)

    exact = service.search("B000EXACT1")
    spanish = service.search("raton inalambrico")
    japanese = service.search("ワイヤレス マウス")

    assert [hit.product.product_id for hit in exact.hits] == ["B000EXACT1"]
    assert [hit.product.product_id for hit in spanish.hits] == ["B000SPAN4"]
    assert [hit.product.product_id for hit in japanese.hits] == ["B000JAPAN5"]
    assert service.search("no such product terms").hits == ()


def test_query_syntax_is_tokenized_instead_of_executed(
    catalog_index: Path,
) -> None:
    service = CatalogSearchService(catalog_index)
    result = service.search('wireless OR "mouse"')

    # OR is treated as an ordinary required token, so FTS syntax cannot be injected.
    assert result.hits == ()


@pytest.mark.parametrize(
    ("query", "top_k"),
    [
        ("   ", 10),
        ("___", 10),
        (" ".join(f"word{index}" for index in range(17)), 10),
        ("mouse", 0),
        ("mouse", 21),
        ("mouse", True),
    ],
)
def test_invalid_public_query_contract_is_rejected(
    catalog_index: Path, query: str, top_k: int
) -> None:
    service = CatalogSearchService(catalog_index)
    with pytest.raises(InvalidCatalogQuery):
        service.search(query, top_k=top_k)


def test_batch_preflights_every_query_before_first_search(
    catalog_index: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CatalogSearchService(catalog_index)
    executed = 0

    def record(*_args, **_kwargs):
        nonlocal executed
        executed += 1
        raise AssertionError("no Query should execute after failed preflight")

    monkeypatch.setattr(service, "_search_tokens", record)
    invalid = " ".join(f"word{index}" for index in range(17))
    with pytest.raises(InvalidCatalogQuery, match="at most 16"):
        service.search_many(["mouse", invalid])
    assert executed == 0


@pytest.mark.parametrize("queries", ["mouse", ["mouse", 3], (), []])
def test_batch_requires_a_nonempty_text_sequence(
    catalog_index: Path,
    queries,
) -> None:
    service = CatalogSearchService(catalog_index)
    with pytest.raises(InvalidCatalogQuery):
        service.search_many(queries)


def test_batch_sql_deadline_interrupts_the_active_query(
    catalog_index: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CatalogSearchService(catalog_index)

    def deliberately_slow(connection, **_kwargs):
        connection.execute(
            "WITH RECURSIVE counter(x) AS ("
            "SELECT 1 UNION ALL SELECT x + 1 FROM counter WHERE x < 100000000"
            ") SELECT sum(x) FROM counter"
        ).fetchone()
        raise AssertionError("deadline did not interrupt SQL")

    monkeypatch.setattr(service, "_search_tokens", deliberately_slow)
    with pytest.raises(CatalogSearchDeadlineExceeded, match="deadline"):
        service.search_many(
            ["mouse"],
            max_elapsed_ms=100,
            max_query_elapsed_ms=1,
        )


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (sqlite3.DatabaseError("database failed"), CatalogBatchSearchFailed),
        (
            CatalogSearchDeadlineExceeded("deadline"),
            CatalogSearchDeadlineExceeded,
        ),
    ],
)
def test_mid_batch_failure_reports_exact_completed_query_count(
    catalog_index: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_type: type[Exception],
) -> None:
    service = CatalogSearchService(catalog_index)
    original = service._search_tokens
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_search_tokens", fail_second)
    with pytest.raises(expected_type) as captured:
        service.search_many(["mouse", "mouse", "mouse"])
    assert captured.value.completed_query_count == 1


def test_same_source_and_revision_have_stable_identity_and_results(
    catalog_source: Path, tmp_path: Path
) -> None:
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    first_metadata = _build(catalog_source, first)
    second_metadata = _build(catalog_source, second)

    assert first_metadata == second_metadata
    assert CatalogSearchService(first).search("wireless mouse").to_dict() == (
        CatalogSearchService(second).search("wireless mouse").to_dict()
    )


def test_failed_rebuild_preserves_previous_index_and_cleans_temporary_file(
    catalog_source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "catalog.sqlite3"
    original_metadata = _build(catalog_source, output)
    original_bytes = output.read_bytes()

    with pytest.raises(ValueError, match="product count"):
        _build(catalog_source, output, expected_count=7)

    assert output.read_bytes() == original_bytes
    assert CatalogSearchService(output).metadata == original_metadata
    assert list(tmp_path.glob(".catalog.sqlite3.*.tmp")) == []


def test_corrupt_metadata_and_symlink_are_rejected(
    catalog_index: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(catalog_index) as connection:
        connection.execute("DELETE FROM catalog_metadata WHERE key = 'index_id'")
        connection.commit()
    with pytest.raises(ValueError, match="metadata contract"):
        CatalogSearchService(catalog_index)

    valid_index = tmp_path / "valid.sqlite3"
    source = tmp_path / "valid-products.parquet"
    pl.DataFrame(
        {
            "product_id": ["B1"],
            "product_locale": ["us"],
            "product_title": ["Mouse"],
            "product_brand": ["Acme"],
            "product_color": ["Black"],
        }
    ).write_parquet(source)
    source_size, source_sha256 = _identity(source)
    build_catalog_index(
        source,
        valid_index,
        expected_source_size=source_size,
        expected_source_sha256=source_sha256,
        expected_product_count=1,
        code_revision=REVISION,
    )
    link = tmp_path / "catalog-link.sqlite3"
    link.symlink_to(valid_index)
    with pytest.raises(ValueError, match="non-symlink"):
        CatalogSearchService(link)


def test_format_valid_but_forged_index_id_is_rejected(catalog_index: Path) -> None:
    with sqlite3.connect(catalog_index) as connection:
        connection.execute(
            "UPDATE catalog_metadata SET value = ? WHERE key = 'index_id'",
            ("catalog-baseline-v1-deadbeefdead",),
        )
        connection.commit()
    with pytest.raises(ValueError, match="metadata identity"):
        CatalogSearchService(catalog_index)


def test_batch_rejects_index_atomically_replaced_after_service_init(
    catalog_source: Path,
    catalog_index: Path,
    tmp_path: Path,
) -> None:
    service = CatalogSearchService(catalog_index)
    replacement_source = tmp_path / "replacement-products.parquet"
    pl.read_parquet(catalog_source).with_columns(
        pl.when(pl.col("product_id") == "B000EXACT1")
        .then(pl.lit("Changed Wireless Mouse"))
        .otherwise(pl.col("product_title"))
        .alias("product_title")
    ).write_parquet(replacement_source)
    replacement_index = tmp_path / "replacement.sqlite3"
    _build(replacement_source, replacement_index)
    replacement_index.replace(catalog_index)

    with pytest.raises(CatalogBatchSearchFailed, match="identity changed") as captured:
        service.search_many(["wireless mouse"])
    assert captured.value.completed_query_count == 0
    with pytest.raises(RuntimeError, match="identity changed"):
        service.search("wireless mouse")


def test_catalog_debug_logs_are_structured_and_do_not_leak_query_or_products(
    catalog_source: Path, tmp_path: Path
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"catalog": "DEBUG"},
        stream=stream,
    )
    output = tmp_path / "catalog.sqlite3"
    metadata = _build(catalog_source, output)
    CatalogSearchService(output).search("Acme Wireless Mouse")

    contents = stream.getvalue()
    assert "Acme Wireless Mouse" not in contents
    assert "B000EXACT1" not in contents
    assert str(catalog_source) not in contents
    events = [json.loads(line) for line in contents.splitlines()]
    assert "catalog_index_build_completed" in {event["event"] for event in events}
    completed = next(
        event for event in events if event["event"] == "catalog_search_completed"
    )
    assert completed["index_id"] == metadata.index_id
    assert completed["query_token_count"] == 3
    assert completed["returned_at_k"] == 1
    assert completed["duration_ms"] >= 0
