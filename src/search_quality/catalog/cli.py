"""Build the full ESCI product index used by the website baseline search."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from search_quality.catalog.index import (
    DEFAULT_CATALOG_INDEX,
    EXPECTED_PRODUCT_COUNT,
    build_catalog_index,
    load_product_lock,
)
from search_quality.evaluation.artifacts import require_clean_code_revision
from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
    logging_context,
    new_trace_id,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("search_quality.catalog")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_ROOT
        / "data/raw/esci/shopping_queries_dataset_products.parquet",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=PROJECT_ROOT / "data/esci.lock.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / DEFAULT_CATALOG_INDEX,
    )
    parser.add_argument("--batch-size", type=int, default=10_000)
    add_logging_arguments(parser)
    return parser


def _execute(args: argparse.Namespace) -> None:
    revision = require_clean_code_revision(PROJECT_ROOT)
    expected_size, expected_sha256 = load_product_lock(args.lock)
    metadata = build_catalog_index(
        args.source,
        args.output,
        expected_source_size=expected_size,
        expected_source_sha256=expected_sha256,
        expected_product_count=EXPECTED_PRODUCT_COUNT,
        code_revision=revision,
        batch_size=args.batch_size,
    )
    print(
        f"{metadata.index_id} | {metadata.product_count} products | "
        f"locales={metadata.locale_counts}"
    )
    print(f"Catalog index: {args.output}")


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    with logging_context(
        trace_id=new_trace_id(),
        operation="catalog_index_build",
    ):
        logger.info("catalog_index_command_started")
        try:
            _execute(args)
        except Exception as exc:
            logger.error(
                "catalog_index_command_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
