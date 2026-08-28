"""CLI for the smoke-only, source-bounded Query constructor."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
    logging_context,
    new_trace_id,
)

from .builder import build_smoke_query_set, store_query_set

PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("search_quality.query_constructor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_logging_arguments(parser)
    return parser


def _execute(args: argparse.Namespace) -> dict[str, object]:
    artifact = build_smoke_query_set(
        project_root=PROJECT_ROOT,
        source_profile="smoke",
    )
    path = store_query_set(artifact, artifact_root=PROJECT_ROOT / "runs")
    return {
        "artifact_path": str(path),
        "deduplicated_count": artifact.deduplicated_count,
        "formal_evaluation_allowed": artifact.formal_evaluation_allowed,
        "original_count": artifact.original_count,
        "query_count": artifact.query_count,
        "query_set_id": artifact.query_set_id,
        "synthetic_count": artifact.synthetic_count,
    }


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    with logging_context(
        trace_id=new_trace_id(),
        operation="source_bounded_query_construction",
    ):
        try:
            summary = _execute(args)
        except Exception as exc:
            logger.error(
                "query_constructor_command_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise SystemExit(1) from None
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
