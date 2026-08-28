"""Run the fixed smoke-only Stage 5 Agent Evaluation Harness."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
    logging_context,
    new_trace_id,
)

from .runner import run_agent_eval_suite

PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("search_quality.agent_eval")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("stage5-retrieval-v1",),
        default="stage5-retrieval-v1",
    )
    add_logging_arguments(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    started = time.perf_counter()
    with logging_context(
        trace_id=new_trace_id(),
        operation="agent_evaluation_harness",
    ):
        try:
            result = run_agent_eval_suite(
                project_root=PROJECT_ROOT,
                suite_id=args.suite,
            )
        except Exception as exc:
            logger.error(
                "agent_eval_command_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                    "failure_stage": "execute_suite",
                    "duration_ms": round(
                        (time.perf_counter() - started) * 1000.0,
                        3,
                    ),
                },
            )
            raise SystemExit(1) from None
    summary = {
        "evidence_id": result.evidence.evidence_id,
        "evidence_path": result.evidence_path,
        "execution_id": result.execution.execution_id,
        "execution_path": result.execution_path,
        "formal_passed": result.evidence.formal_passed,
        "metrics": result.evidence.metrics.model_dump(mode="json"),
        "task_count": len(result.evidence.tasks),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if not result.evidence.formal_passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
