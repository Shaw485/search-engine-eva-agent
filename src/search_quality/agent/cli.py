"""Run the deterministic smoke-only Agent Runtime vertical slice."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
)

from .contracts import AgentTask
from .planner import FakeBranchingPlanner
from .runtime import AgentRuntime
from .tools import SearchEvaluationTools, TrustedRunRegistry
from .trace import TraceStore

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUN_STORE = PROJECT_ROOT / "runs"
TRACE_STORE = RUN_STORE / "agent-traces"
logger = logging.getLogger("search_quality.agent_runtime")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run-id", required=True)
    parser.add_argument("--candidate-run-id", required=True)
    parser.add_argument(
        "--max-regressions-to-inspect",
        type=int,
        choices=(0, 1, 2, 3),
        default=1,
    )
    add_logging_arguments(parser)
    return parser


def _execute(args: argparse.Namespace):
    task = AgentTask(
        task_id="smoke-run-comparison",
        baseline_run_id=args.baseline_run_id,
        candidate_run_id=args.candidate_run_id,
        max_regressions_to_inspect=args.max_regressions_to_inspect,
    )
    run_registry = TrustedRunRegistry(
        store_root=RUN_STORE,
        project_root=PROJECT_ROOT,
        manifest_path=PROJECT_ROOT / "data/manifests/esci-stage1.json",
        allowed_run_ids=(task.baseline_run_id, task.candidate_run_id),
    )
    tools = SearchEvaluationTools(
        project_root=PROJECT_ROOT,
        registry=run_registry,
    ).build_registry()
    runtime = AgentRuntime(
        planner=FakeBranchingPlanner(),
        tools=tools,
        trace_store=TraceStore(TRACE_STORE),
    )
    return runtime.run(task)


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    try:
        result = _execute(args)
    except Exception as exc:
        logger.error(
            "agent_command_failed",
            extra={
                "error_code": classify_error(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
