"""CLI for the fixed 59-Query label-blind Bad Case diagnostic batch."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from search_quality.catalog import DEFAULT_CATALOG_INDEX
from search_quality.evaluation.artifacts import require_clean_code_revision
from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
    current_trace_id,
    logging_context,
    new_trace_id,
)

from .artifacts import BadCaseRunInProgress
from .supervisor import (
    DEFAULT_WORKER_DEADLINE_MS,
    BadCaseWorkerDeadlineExceeded,
    BadCaseWorkerError,
    load_supervisor_execution_receipt,
    supervise_bad_case_diagnostics,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("search_quality.bad_case")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_logging_arguments(parser)
    return parser


def _execute(_args: argparse.Namespace) -> dict[str, object]:
    run = supervise_bad_case_diagnostics(
        project_root=PROJECT_ROOT,
        artifact_root=None,
        catalog_index_path=PROJECT_ROOT / DEFAULT_CATALOG_INDEX,
        executor_revision=require_clean_code_revision(PROJECT_ROOT),
        deadline_ms=DEFAULT_WORKER_DEADLINE_MS,
        trace_id=current_trace_id(),
    )
    artifact = run.artifact
    supervisor_receipt = load_supervisor_execution_receipt(
        PROJECT_ROOT / "runs",
        run.execution.execution_id,
    )
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
        "supervisor_receipt_id": supervisor_receipt.receipt_id,
        "worker_deadline_ms": supervisor_receipt.deadline_ms,
        "worker_policy_id": supervisor_receipt.policy_id,
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
        except BadCaseRunInProgress as exc:
            logger.warning("bad_case_command_busy")
            raise SystemExit(3) from exc
        except BadCaseWorkerDeadlineExceeded as exc:
            logger.error(
                "bad_case_command_deadline_exceeded",
                extra={
                    "error_code": exc.error_code,
                    "execution_id": exc.execution_id,
                },
            )
            raise SystemExit(4) from None
        except BadCaseWorkerError as exc:
            logger.error(
                "bad_case_command_worker_failed",
                extra={
                    "error_code": exc.error_code,
                    "execution_id": exc.execution_id,
                },
            )
            raise SystemExit(5) from None
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
