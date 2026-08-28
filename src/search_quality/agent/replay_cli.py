"""Replay one stored Agent Trace without invoking a Planner or tool."""

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

from .replay import TraceReplayer
from .trace import TraceStore

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TRACE_STORE = PROJECT_ROOT / "runs" / "agent-traces"
logger = logging.getLogger("search_quality.agent_replay")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_id")
    add_logging_arguments(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    try:
        result = TraceReplayer(TraceStore(TRACE_STORE)).replay(args.trace_id)
    except Exception as exc:
        logger.error(
            "agent_replay_command_failed",
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
