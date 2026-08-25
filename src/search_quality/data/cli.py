"""Command-line entry point for the Stage 1 ESCI build."""

from __future__ import annotations

import argparse
from pathlib import Path

from search_quality.data.contracts import (
    Stage1Config,
    load_dataset_lock,
    validate_source_files,
)
from search_quality.data.esci import build_stage1

PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = Stage1Config.from_path(args.config)
    lock = load_dataset_lock(args.lock)
    if args.validate_only:
        validate_source_files(args.source_dir, lock)
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
    print(
        "Stage 1 ESCI build complete: "
        f"{manifest['profiles']['smoke']['queries']} smoke queries, "
        f"{manifest['profiles']['dev']['queries']} dev queries, "
        f"{manifest['profiles']['test']['queries']} frozen test queries"
    )


if __name__ == "__main__":
    main()
