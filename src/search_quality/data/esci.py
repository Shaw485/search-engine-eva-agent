"""Build the validated English ESCI Stage 1 dataset."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import polars as pl

from search_quality.data.contracts import (
    DataContractError,
    DatasetLock,
    Stage1Config,
    canonical_json_sha256,
    resolve_source_paths,
    sha256_file,
    validate_source_files,
)
from search_quality.data.splits import QueryIdentity, plan_query_splits

EXAMPLES_FILE = "shopping_queries_dataset_examples.parquet"
PRODUCTS_FILE = "shopping_queries_dataset_products.parquet"
SOURCES_FILE = "shopping_queries_dataset_sources.csv"

EXAMPLE_COLUMNS = {
    "example_id",
    "query",
    "query_id",
    "product_id",
    "product_locale",
    "esci_label",
    "small_version",
    "large_version",
    "split",
}
PRODUCT_COLUMNS = {
    "product_id",
    "product_title",
    "product_description",
    "product_bullet_point",
    "product_brand",
    "product_color",
    "product_locale",
}
SOURCE_COLUMNS = {"query_id", "source"}
PAIR_KEYS = ["product_locale", "query_id", "product_id"]
PRODUCT_KEYS = ["product_locale", "product_id"]
OUTPUT_COLUMNS = [
    "example_id",
    "query_id",
    "query_text",
    "query_key",
    "product_id",
    "product_locale",
    "product_title",
    "product_description",
    "product_bullet_point",
    "product_brand",
    "product_color",
    "esci_label",
    "source",
    "origin_split",
    "eval_split",
    "is_smoke",
]


@dataclass(frozen=True, slots=True)
class CleaningStats:
    input_judgments: int
    duplicate_judgments_removed: int
    empty_title_judgments_quarantined: int
    missing_source_queries: int


@dataclass(frozen=True, slots=True)
class PreparedStage1:
    train: pl.DataFrame
    dev: pl.DataFrame
    test: pl.DataFrame
    smoke: pl.DataFrame
    quarantine: pl.DataFrame
    cleaning: CleaningStats


def _require_columns(
    frame: pl.LazyFrame, required: set[str], *, table_name: str
) -> None:
    available = set(frame.collect_schema().names())
    missing = sorted(required - available)
    if missing:
        raise DataContractError(f"{table_name} is missing columns: {missing}")


def _string_expr(name: str) -> pl.Expr:
    return pl.col(name).cast(pl.String).fill_null("").str.strip_chars()


def _load_examples(path: Path, config: Stage1Config) -> tuple[pl.DataFrame, int]:
    lazy = pl.scan_parquet(path)
    _require_columns(lazy, EXAMPLE_COLUMNS, table_name="examples")
    if config.dataset_version_column not in EXAMPLE_COLUMNS:
        raise DataContractError(
            f"unsupported dataset version column {config.dataset_version_column!r}"
        )
    frame = (
        lazy.filter(
            (pl.col(config.dataset_version_column) == config.dataset_version_value)
            & (pl.col("product_locale") == config.locale)
        )
        .select(
            "example_id",
            "query",
            "query_id",
            "product_id",
            "product_locale",
            "esci_label",
            "split",
        )
        .collect(engine="streaming")
        .with_columns(
            pl.col("example_id").cast(pl.Int64),
            pl.col("query_id").cast(pl.Int64),
            _string_expr("query"),
            _string_expr("product_id"),
            _string_expr("product_locale").str.to_lowercase(),
            _string_expr("esci_label").str.to_uppercase(),
            _string_expr("split").str.to_lowercase(),
        )
    )
    if frame.is_empty():
        raise DataContractError("the configured ESCI slice contains no judgments")
    for column in ("query", "product_id", "product_locale", "esci_label", "split"):
        if frame.filter(pl.col(column) == "").height:
            raise DataContractError(f"examples contains empty {column} values")
    invalid_labels = sorted(
        frame.filter(~pl.col("esci_label").is_in(config.valid_labels))
        .get_column("esci_label")
        .unique()
        .to_list()
    )
    if invalid_labels:
        raise DataContractError(
            f"examples contains invalid ESCI labels: {invalid_labels}"
        )
    valid_splits = {config.official_train_value, config.official_test_value}
    invalid_splits = sorted(
        frame.filter(~pl.col("split").is_in(valid_splits))
        .get_column("split")
        .unique()
        .to_list()
    )
    if invalid_splits:
        raise DataContractError(
            f"examples contains unsupported official splits: {invalid_splits}"
        )

    query_conflicts = (
        frame.group_by("query_id")
        .agg(
            pl.col("query").n_unique().alias("query_texts"),
            pl.col("product_locale").n_unique().alias("locales"),
            pl.col("split").n_unique().alias("origin_splits"),
        )
        .filter(
            (pl.col("query_texts") != 1)
            | (pl.col("locales") != 1)
            | (pl.col("origin_splits") != 1)
        )
    )
    if query_conflicts.height:
        query_id = query_conflicts.row(0, named=True)["query_id"]
        raise DataContractError(f"query_id {query_id} maps to conflicting identities")

    label_conflicts = (
        frame.group_by(PAIR_KEYS)
        .agg(pl.col("esci_label").n_unique().alias("label_count"))
        .filter(pl.col("label_count") != 1)
    )
    if label_conflicts.height:
        sample = label_conflicts.row(0, named=True)
        raise DataContractError(
            "a query-product pair has conflicting ESCI labels: "
            f"query_id={sample['query_id']}, product_id={sample['product_id']}"
        )

    input_rows = frame.height
    frame = frame.sort("example_id").unique(
        subset=PAIR_KEYS, keep="first", maintain_order=True
    )
    return frame, input_rows - frame.height


def _load_products(
    path: Path, *, config: Stage1Config, required_keys: pl.DataFrame
) -> pl.DataFrame:
    lazy = pl.scan_parquet(path)
    _require_columns(lazy, PRODUCT_COLUMNS, table_name="products")
    frame = (
        lazy.filter(pl.col("product_locale") == config.locale)
        .select(sorted(PRODUCT_COLUMNS))
        .join(required_keys.lazy(), on=PRODUCT_KEYS, how="semi")
        .collect(engine="streaming")
        .with_columns(
            _string_expr("product_id"),
            _string_expr("product_locale").str.to_lowercase(),
            *(
                _string_expr(column)
                for column in (
                    "product_title",
                    "product_description",
                    "product_bullet_point",
                    "product_brand",
                    "product_color",
                )
            ),
        )
    )
    if frame.filter(pl.col("product_id") == "").height:
        raise DataContractError("products contains empty product_id values")
    duplicate_keys = frame.group_by(PRODUCT_KEYS).len().filter(pl.col("len") != 1)
    if duplicate_keys.height:
        sample = duplicate_keys.row(0, named=True)
        raise DataContractError(
            "products contains duplicate composite keys: "
            f"locale={sample['product_locale']}, product_id={sample['product_id']}"
        )
    missing = required_keys.join(
        frame.select(PRODUCT_KEYS), on=PRODUCT_KEYS, how="anti"
    )
    if missing.height:
        sample = missing.row(0, named=True)
        raise DataContractError(
            "a judgment has no matching product for the composite key: "
            f"locale={sample['product_locale']}, product_id={sample['product_id']}"
        )
    return frame


def _load_sources(path: Path) -> pl.DataFrame:
    lazy = pl.scan_csv(path)
    _require_columns(lazy, SOURCE_COLUMNS, table_name="sources")
    frame = (
        lazy.select("query_id", "source")
        .collect(engine="streaming")
        .with_columns(
            pl.col("query_id").cast(pl.Int64),
            _string_expr("source").str.to_lowercase(),
        )
    )
    duplicates = frame.group_by("query_id").len().filter(pl.col("len") != 1)
    if duplicates.height:
        query_id = duplicates.row(0, named=True)["query_id"]
        raise DataContractError(f"sources contains duplicate query_id {query_id}")
    return frame


def prepare_stage1(
    *, source_dir: str | Path, config: Stage1Config, lock: DatasetLock
) -> PreparedStage1:
    """Read, validate, join, clean and split one pinned ESCI slice."""

    if config.source_commit != lock.commit:
        raise DataContractError(
            "Stage 1 config and dataset lock reference different source commits"
        )
    paths = resolve_source_paths(source_dir, lock)
    examples, duplicate_count = _load_examples(paths[EXAMPLES_FILE], config)
    required_keys = examples.select(PRODUCT_KEYS).unique().sort(PRODUCT_KEYS)
    products = _load_products(
        paths[PRODUCTS_FILE], config=config, required_keys=required_keys
    )
    sources = _load_sources(paths[SOURCES_FILE])

    joined = examples.join(products, on=PRODUCT_KEYS, how="left", validate="m:1")
    empty_title = joined.filter(pl.col("product_title") == "").with_columns(
        pl.lit("empty_product_title").alias("quarantine_reason")
    )
    clean = joined.filter(pl.col("product_title") != "")
    before_query_ids = set(examples.get_column("query_id").unique().to_list())
    after_query_ids = set(clean.get_column("query_id").unique().to_list())
    lost_queries = sorted(before_query_ids - after_query_ids)
    if lost_queries:
        raise DataContractError(
            "cleaning removed every candidate for at least one query: "
            f"query_id={lost_queries[0]}"
        )

    query_table = clean.select("query_id", "query", "product_locale", "split").unique()
    identities = [
        QueryIdentity(
            query_id=row["query_id"],
            query_text=row["query"],
            locale=row["product_locale"],
            origin_split=row["split"],
        )
        for row in query_table.iter_rows(named=True)
    ]
    split_plan = plan_query_splits(
        identities,
        seed=config.split_seed,
        dev_query_count=config.dev_query_count,
        smoke_query_count=config.smoke_query_count,
        official_train_value=config.official_train_value,
        official_test_value=config.official_test_value,
    )
    assignment_rows = [
        {
            "query_id": query_id,
            "query_key": split_plan.normalized_queries[query_id],
            "eval_split": split,
            "is_smoke": query_id in split_plan.smoke_query_ids,
        }
        for query_id, split in split_plan.assignments.items()
    ]
    assignments = pl.DataFrame(assignment_rows)
    query_sources = query_table.select("query_id").join(
        sources, on="query_id", how="left", validate="1:1"
    )
    missing_source_queries = query_sources.filter(pl.col("source").is_null()).height
    clean = (
        clean.rename({"query": "query_text", "split": "origin_split"})
        .join(assignments, on="query_id", how="left", validate="m:1")
        .join(sources, on="query_id", how="left", validate="m:1")
        .with_columns(pl.col("source").fill_null("unknown"))
        .select(OUTPUT_COLUMNS)
        .sort("eval_split", "query_id", "example_id")
    )
    if clean.filter(pl.col("eval_split").is_null()).height:
        raise DataContractError("one or more judgments have no split assignment")

    outputs = {
        split: clean.filter(pl.col("eval_split") == split).sort(
            "query_id", "example_id"
        )
        for split in ("train", "dev", "test")
    }
    smoke = outputs["dev"].filter(pl.col("is_smoke")).sort("query_id", "example_id")
    if smoke.get_column("query_id").n_unique() < config.smoke_query_count:
        raise DataContractError("smoke profile contains fewer queries than configured")

    return PreparedStage1(
        train=outputs["train"],
        dev=outputs["dev"],
        test=outputs["test"],
        smoke=smoke,
        quarantine=empty_title,
        cleaning=CleaningStats(
            input_judgments=examples.height + duplicate_count,
            duplicate_judgments_removed=duplicate_count,
            empty_title_judgments_quarantined=empty_title.height,
            missing_source_queries=missing_source_queries,
        ),
    )


def canonical_frame_sha256(frame: pl.DataFrame) -> str:
    """Hash logical rows, independent of input order and Parquet encoding."""

    digest = hashlib.sha256()
    ordered = frame.select(OUTPUT_COLUMNS).sort("query_id", "example_id")
    for row in ordered.iter_rows():
        payload = json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(payload)
        digest.update(b"\n")
    return digest.hexdigest()


def _distribution(frame: pl.DataFrame, column: str) -> dict[str, int]:
    rows = frame.group_by(column).len().sort(column).iter_rows()
    return {str(value): count for value, count in rows}


def _percentile(values: list[int], proportion: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * proportion)
    return ordered[index]


def profile_frame(frame: pl.DataFrame) -> dict[str, Any]:
    query_frame = frame.select("query_id", "query_text", "source").unique()
    candidate_counts = frame.group_by("query_id").len().get_column("len").to_list()
    query_tokens = [len(text.split()) for text in query_frame["query_text"].to_list()]
    query_characters = [len(text) for text in query_frame["query_text"].to_list()]
    missing_fields = {}
    for field in (
        "product_description",
        "product_bullet_point",
        "product_brand",
        "product_color",
    ):
        empty_count = frame.filter(pl.col(field) == "").height
        missing_fields[field] = {
            "empty_count": empty_count,
            "empty_rate": round(empty_count / frame.height, 6) if frame.height else 0.0,
        }
    return {
        "rows": frame.height,
        "columns": len(frame.columns),
        "queries": query_frame.height,
        "products": frame.get_column("product_id").n_unique(),
        "labels": _distribution(frame, "esci_label"),
        "query_sources": _distribution(query_frame, "source"),
        "candidate_count": {
            "min": min(candidate_counts, default=0),
            "mean": round(sum(candidate_counts) / len(candidate_counts), 3)
            if candidate_counts
            else 0.0,
            "p50": _percentile(candidate_counts, 0.5),
            "p95": _percentile(candidate_counts, 0.95),
            "max": max(candidate_counts, default=0),
            "queries_over_40": sum(value > 40 for value in candidate_counts),
        },
        "query_token_count": {
            "min": min(query_tokens, default=0),
            "p50": _percentile(query_tokens, 0.5),
            "p95": _percentile(query_tokens, 0.95),
            "max": max(query_tokens, default=0),
        },
        "query_character_count": {
            "min": min(query_characters, default=0),
            "p50": _percentile(query_characters, 0.5),
            "p95": _percentile(query_characters, 0.95),
            "max": max(query_characters, default=0),
        },
        "field_completeness": missing_fields,
        "canonical_sha256": canonical_frame_sha256(frame),
    }


def _git_revision(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def build_stage1(
    *,
    source_dir: str | Path,
    output_dir: str | Path,
    config: Stage1Config,
    lock: DatasetLock,
    manifest_path: str | Path,
    report_path: str | Path,
    project_root: str | Path,
) -> dict[str, Any]:
    """Validate sources, materialize Parquet outputs and write evidence."""

    source_evidence = validate_source_files(source_dir, lock)
    prepared = prepare_stage1(source_dir=source_dir, config=config, lock=lock)
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    frames = {
        "train": prepared.train,
        "dev": prepared.dev,
        "test": prepared.test,
        "smoke": prepared.smoke,
    }
    profiles = {name: profile_frame(frame) for name, frame in frames.items()}
    output_evidence: dict[str, dict[str, str | int]] = {}
    for name, frame in frames.items():
        target = output_root / f"{name}.parquet"
        temporary = output_root / f".{name}.parquet.tmp"
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        temporary.replace(target)
        output_evidence[name] = {
            "path": target.name,
            "size": target.stat().st_size,
            "file_sha256": sha256_file(target),
            "canonical_sha256": profiles[name]["canonical_sha256"],
        }
    if prepared.quarantine.height:
        quarantine_path = output_root / "quarantine.parquet"
        prepared.quarantine.write_parquet(
            quarantine_path, compression="zstd", statistics=True
        )

    manifest_target = Path(manifest_path)
    previous_outputs: dict[str, Any] | None = None
    if manifest_target.is_file():
        try:
            previous_payload = json.loads(manifest_target.read_text(encoding="utf-8"))
            previous_outputs = previous_payload.get("outputs")
        except (json.JSONDecodeError, OSError):
            previous_outputs = None
    output_fingerprints = {
        name: {
            "file_sha256": evidence["file_sha256"],
            "canonical_sha256": evidence["canonical_sha256"],
        }
        for name, evidence in output_evidence.items()
    }
    previous_fingerprints = (
        {
            name: {
                "file_sha256": evidence.get("file_sha256"),
                "canonical_sha256": evidence.get("canonical_sha256"),
            }
            for name, evidence in previous_outputs.items()
        }
        if isinstance(previous_outputs, dict)
        else None
    )

    manifest = {
        "schema_version": config.schema_version,
        "source": {
            "repository": lock.repository,
            "commit": lock.commit,
            "license": lock.license,
            "files": source_evidence,
        },
        "config": config.to_dict(),
        "config_sha256": canonical_json_sha256(config.to_dict()),
        "code_revision": _git_revision(Path(project_root)),
        "cleaning": asdict(prepared.cleaning),
        "profiles": profiles,
        "outputs": output_evidence,
        "reproducibility": {
            "previous_manifest_available": previous_fingerprints is not None,
            "outputs_identical_to_previous": previous_fingerprints
            == output_fingerprints
            if previous_fingerprints is not None
            else None,
        },
        "evaluation_boundary": {
            "primary_track": "judged-candidate reranking",
            "unjudged_products_are_irrelevant": False,
            "full_catalog_recall_claimed": False,
            "smoke_is_formal_split": False,
        },
    }
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    manifest_target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_target = Path(report_path)
    report_target.parent.mkdir(parents=True, exist_ok=True)
    report_target.write_text(render_report(manifest), encoding="utf-8")
    return manifest


def render_report(manifest: dict[str, Any]) -> str:
    profiles = manifest["profiles"]
    lines = [
        "# Stage 1 ESCI data report",
        "",
        f"- Schema: `{manifest['schema_version']}`",
        f"- Source commit: `{manifest['source']['commit']}`",
        "- Slice: English-US, Task 1 reduced set",
        "- Grain: one row per judged Query-product pair",
        "",
        "## Split overview",
        "",
        "| Asset | Formal split | Queries | Rows | Products |",
        "|---|---|---:|---:|---:|",
    ]
    for name in ("train", "dev", "test", "smoke"):
        profile = profiles[name]
        formal = "dev profile" if name == "smoke" else name
        lines.append(
            f"| {name} | {formal} | {profile['queries']} | "
            f"{profile['rows']} | {profile['products']} |"
        )
    lines.extend(
        [
            "",
            "## Data quality",
            "",
            f"- Duplicate judgments removed: "
            f"{manifest['cleaning']['duplicate_judgments_removed']}",
            f"- Empty-title judgments quarantined: "
            f"{manifest['cleaning']['empty_title_judgments_quarantined']}",
            f"- Queries missing a source category: "
            f"{manifest['cleaning']['missing_source_queries']}",
            f"- Repeated build matched previous logical and file hashes: "
            f"{manifest['reproducibility']['outputs_identical_to_previous']}.",
            "- Formal split leakage: checked by Query ID and normalized Query text.",
            "- Product join key: `(product_locale, product_id)`.",
            "",
            "## Label distribution",
            "",
            "| Split | E | S | C | I |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name in ("train", "dev", "test"):
        labels = profiles[name]["labels"]
        lines.append(
            f"| {name} | {labels.get('E', 0)} | {labels.get('S', 0)} | "
            f"{labels.get('C', 0)} | {labels.get('I', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Shape and completeness",
            "",
            "| Split | Candidates p50 / p95 / max | Query tokens p50 / p95 | "
            "Empty description | Empty bullet | Empty brand | Empty color |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name in ("train", "dev", "test"):
        profile = profiles[name]
        candidates = profile["candidate_count"]
        tokens = profile["query_token_count"]
        missing = profile["field_completeness"]
        lines.append(
            f"| {name} | {candidates['p50']} / {candidates['p95']} / "
            f"{candidates['max']} | {tokens['p50']} / {tokens['p95']} | "
            f"{missing['product_description']['empty_rate']:.1%} | "
            f"{missing['product_bullet_point']['empty_rate']:.1%} | "
            f"{missing['product_brand']['empty_rate']:.1%} | "
            f"{missing['product_color']['empty_rate']:.1%} |"
        )
    lines.extend(
        [
            "",
            "### Observed quality notes",
            "",
            "- Product descriptions are sparse at roughly 49–50% empty; ranking "
            "templates must not depend on descriptions alone.",
            "- Product color is empty for roughly 30% of judgments; color-aware "
            "analysis must report this coverage limit.",
            f"- Queries with more than 40 judged candidates were observed: train "
            f"{profiles['train']['candidate_count']['queries_over_40']}, dev "
            f"{profiles['dev']['candidate_count']['queries_over_40']}, test "
            f"{profiles['test']['candidate_count']['queries_over_40']}. The pipeline "
            "preserves these official rows instead of enforcing the README's "
            "informal 'up to 40' description.",
            "",
            "## Evaluation boundary",
            "",
            "ESCI labels cover judged candidates for each Query, not the entire Amazon "
            "catalog. Unjudged products are **unknown**, not automatically Irrelevant. "
            "The primary benchmark therefore evaluates candidate-set reranking. This "
            "report does not claim full-catalog Recall.",
            "",
            "The official dataset has no category field. Stage 1 preserves the official "
            "bullet point, brand and color fields and does not fabricate a category.",
            "",
        ]
    )
    return "\n".join(lines)
