"""Command-line entry point for the Stage 1 ESCI build."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from search_quality.data.contracts import (
    Stage1Config,
    load_dataset_lock,
    validate_source_files,
)
from search_quality.data.esci import build_stage1
from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
    logging_context,
    new_trace_id,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("search_quality.data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir", default=PROJECT_ROOT / "data" / "raw" / "esci", type=Path
    )
    parser.add_argument(
        "--output-dir",
        default=PROJECT_ROOT / "data" / "processed" / "esci-stage1-v1",
        type=Path,
    )
    parser.add_argument(
        "--config",
        default=PROJECT_ROOT / "configs" / "data" / "esci-stage1.json",
        type=Path,
    )
    parser.add_argument(
        "--lock", default=PROJECT_ROOT / "data" / "esci.lock.json", type=Path
    )
    parser.add_argument(
        "--manifest",
        default=PROJECT_ROOT / "data" / "manifests" / "esci-stage1.json",
        type=Path,
    )
    parser.add_argument(
        "--report",
        default=PROJECT_ROOT / "docs" / "STAGE_1_REPORT.md",
        type=Path,
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="verify pinned files without materializing derived data",
    )
    add_logging_arguments(parser)
    return parser


def _execute(args: argparse.Namespace) -> None:
    config = Stage1Config.from_path(args.config)
    lock = load_dataset_lock(args.lock)
    if args.validate_only:
        validate_source_files(args.source_dir, lock)
        logger.info("source_validation_completed", extra={"source_commit": lock.commit})
        print(f"ESCI source validation passed for commit {lock.commit}")
        return
    manifest = build_stage1(
        source_dir=args.source_dir,
        output_dir=args.output_dir,
        config=config,
        lock=lock,
        manifest_path=args.manifest,
        report_path=args.report,
        project_root=PROJECT_ROOT,
    )
    logger.info(
        "stage1_build_completed",
        extra={
            "dev_queries": manifest["profiles"]["dev"]["queries"],
            "smoke_queries": manifest["profiles"]["smoke"]["queries"],
            "test_queries": manifest["profiles"]["test"]["queries"],
        },
    )
    print(
        "Stage 1 ESCI build complete: "
        f"{manifest['profiles']['smoke']['queries']} smoke queries, "
        f"{manifest['profiles']['dev']['queries']} dev queries, "
        f"{manifest['profiles']['test']['queries']} frozen test queries"
    )


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    operation = "source_validation" if args.validate_only else "stage1_build"
    with logging_context(trace_id=new_trace_id(), operation=operation):
        logger.info("data_command_started")
        try:
            _execute(args)
        except Exception as exc:
            logger.error(
                "data_command_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
