"""Compare two compatible evaluation Runs from the trusted local Run store."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from search_quality.evaluation.artifacts import (
    atomic_write_text,
    require_clean_code_revision,
    write_immutable_json,
    write_immutable_text,
)
from search_quality.evaluation.authorization import ensure_profile_authorized
from search_quality.evaluation.comparison import (
    METRIC_NAMES,
    compare_runs,
    load_run_from_store,
    render_comparison_markdown,
)
from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
    logging_context,
    new_trace_id,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUN_STORE_ROOT = PROJECT_ROOT / "runs"
logger = logging.getLogger("search_quality.evaluation")


def _set_diagnostic_stage(args: argparse.Namespace, stage: str) -> None:
    """Record one allowlisted stage for a later safe command-failure event."""

    args._diagnostic_stage = stage


def _comparison_output_dir(path: Path) -> Path:
    if RUN_STORE_ROOT.is_symlink():
        raise ValueError("trusted local Run store must not be a symbolic link")
    root = RUN_STORE_ROOT.resolve(strict=True)
    comparison_dir = root / "comparisons"
    if comparison_dir.is_symlink() or path.is_symlink():
        raise ValueError("comparison output must not be a symbolic link")
    if ".." in path.parts:
        raise ValueError("comparison output must not contain parent traversal")
    expected = comparison_dir.resolve(strict=False)
    if expected.parent != root:
        raise ValueError("comparison output resolves outside the trusted Run store")
    resolved = path.resolve(strict=False)
    if resolved != expected:
        raise ValueError("comparison output must use the trusted Run store")
    return resolved


def _run_input_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    baseline = args.baseline or RUN_STORE_ROOT / f"latest-{args.profile}-random.txt"
    candidate = (
        args.candidate or RUN_STORE_ROOT / f"latest-{args.profile}-title-bm25.txt"
    )
    return baseline, candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("smoke", "dev"), default="smoke")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="baseline Run JSON/pointer; defaults to latest PROFILE random Run",
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        default=None,
        help="candidate Run JSON/pointer; defaults to latest PROFILE title-BM25 Run",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RUN_STORE_ROOT / "comparisons",
    )
    add_logging_arguments(parser)
    return parser


def _execute(args: argparse.Namespace) -> None:
    _set_diagnostic_stage(args, "authorization")
    ensure_profile_authorized(args.profile)
    _set_diagnostic_stage(args, "revision")
    revision = require_clean_code_revision(PROJECT_ROOT)
    _set_diagnostic_stage(args, "load")
    baseline_path, candidate_path = _run_input_paths(args)
    baseline = load_run_from_store(baseline_path, store_root=RUN_STORE_ROOT)
    candidate = load_run_from_store(candidate_path, store_root=RUN_STORE_ROOT)
    _set_diagnostic_stage(args, "validate")
    for run, role in ((baseline, "baseline"), (candidate, "candidate")):
        dataset = run.get("dataset")
        if not isinstance(dataset, dict) or dataset.get("profile") != args.profile:
            raise ValueError(f"{role} Run does not match the requested profile")

    _set_diagnostic_stage(args, "compare")
    comparison = compare_runs(
        baseline,
        candidate,
        comparator_revision=revision,
        expected_profile=args.profile,
        project_root=PROJECT_ROOT,
        manifest_path=PROJECT_ROOT / "data/manifests/esci-stage1.json",
    )
    comparison_id = comparison["comparison_id"]
    report = render_comparison_markdown(comparison)
    _set_diagnostic_stage(args, "store")
    output_dir = _comparison_output_dir(args.output_dir)
    json_path = output_dir / f"{comparison_id}.json"
    markdown_path = output_dir / f"{comparison_id}.md"
    write_immutable_json(json_path, comparison)
    write_immutable_text(markdown_path, report)
    atomic_write_text(
        output_dir / f"latest-{args.profile}-comparison.txt",
        json_path.name + "\n",
    )
    logger.info(
        "comparison_artifacts_stored",
        extra={
            "baseline_run_id": comparison["baseline"]["run_id"],
            "candidate_run_id": comparison["candidate"]["run_id"],
            "comparison_id": comparison_id,
            "profile_id": args.profile,
        },
    )

    print(
        f"{comparison_id} | {comparison['compatibility']['query_count']} queries | "
        f"candidate={comparison['candidate']['run_id']} minus "
        f"baseline={comparison['baseline']['run_id']}"
    )
    for metric in METRIC_NAMES:
        values = comparison["aggregate_metrics"][metric]
        print(
            f"{metric}: {values['baseline']:.6f} -> "
            f"{values['candidate']:.6f} ({values['delta']:+.6f})"
        )
    print(f"Comparison JSON: {json_path}")
    print(f"Comparison report: {markdown_path}")


def main() -> None:
    args = build_parser().parse_args()
    _set_diagnostic_stage(args, "execute")
    configure_logging_from_args(args)
    with logging_context(
        trace_id=new_trace_id(),
        operation="stage2_compare_runs",
        profile_id=args.profile,
    ):
        logger.info("comparison_command_started")
        try:
            _execute(args)
        except Exception as exc:
            logger.error(
                "comparison_command_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                    "failure_stage": args._diagnostic_stage,
                },
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
