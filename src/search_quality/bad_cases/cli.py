"""CLI for the fixed 59-Query label-blind Bad Case diagnostic batch."""

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

from .runner import run_bad_case_diagnostics

PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("search_quality.bad_case")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_logging_arguments(parser)
    return parser


def _execute(_args: argparse.Namespace) -> dict[str, object]:
    run = run_bad_case_diagnostics(project_root=PROJECT_ROOT)
    artifact = run.artifact
    return {
        "completed": artifact.completed,
        "diagnostic_candidate_count": artifact.diagnostic_candidate_count,
        "diagnostic_id": artifact.diagnostic_id,
        "execution_id": run.execution.execution_id,
        "formal_evaluation_allowed": artifact.formal_evaluation_allowed,
        "quality_metrics_computed": artifact.quality_metrics_computed,
        "query_count": artifact.query_count,
        "query_set_id": artifact.query_set_id,
        "strategy_write_count": artifact.strategy_write_count,
    }


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    with logging_context(
        trace_id=new_trace_id(),
        operation="source_bounded_bad_case_diagnostics",
    ):
        try:
            summary = _execute(args)
        except Exception as exc:
            logger.error(
                "bad_case_command_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise SystemExit(1) from None
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
