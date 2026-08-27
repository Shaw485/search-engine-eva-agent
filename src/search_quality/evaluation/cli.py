"""Run deterministic Stage 2 baselines on an authorized safe profile."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from search_quality.evaluation.authorization import ensure_profile_authorized
from search_quality.evaluation.baseline import (
    DEFAULT_RANDOM_SEED,
    RANKER_NAMES,
    run_candidate_baseline,
)
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.observability import (
    add_logging_arguments,
    classify_error,
    configure_logging_from_args,
    logging_context,
    new_trace_id,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("search_quality.evaluation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("smoke", "dev"), default="smoke")
    parser.add_argument(
        "--ranker",
        choices=(*RANKER_NAMES, "all"),
        default="all",
        help="run one comparator or all deterministic Stage 2 baselines",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "data" / "manifests" / "esci-stage1.json",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=PROJECT_ROOT / "configs" / "evaluation" / "esci-primary-v1.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "runs",
    )
    add_logging_arguments(parser)
    return parser


def _code_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    revision = completed.stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(
            "formal evaluation requires a clean Git worktree; commit or stash "
            "changes before running"
        )
    return revision


def ensure_profile_unlocked(profile_id: str) -> None:
    """Backward-compatible alias for the shared formal-run authorization gate."""

    ensure_profile_authorized(profile_id)


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _write_run(
    run: dict,
    *,
    output_dir: Path,
    profile_id: str,
    ranker_name: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{run['run_id']}.json"
    serialized = json.dumps(run, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output.is_file() and output.read_text(encoding="utf-8") != serialized:
        raise RuntimeError(f"immutable Run ID collision at {output}")
    if not output.is_file():
        _atomic_write_text(output, serialized)
    latest = output_dir / f"latest-{profile_id}-{ranker_name}.txt"
    _atomic_write_text(latest, output.name + "\n")
    logger.info(
        "run_manifest_stored",
        extra={
            "profile_id": profile_id,
            "ranker_name": ranker_name,
            "run_id": run["run_id"],
        },
    )
    return output


def _execute(args: argparse.Namespace) -> None:
    ensure_profile_unlocked(args.profile)
    policy = RelevancePolicy.from_path(args.policy)
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id=args.profile,
        project_root=PROJECT_ROOT,
        manifest_path=args.manifest,
    )
    revision = _code_revision()
    ranker_names = RANKER_NAMES if args.ranker == "all" else (args.ranker,)
    for ranker_name in ranker_names:
        run = run_candidate_baseline(
            profile,
            policy=policy,
            code_revision=revision,
            ranker_name=ranker_name,
            random_seed=DEFAULT_RANDOM_SEED,
        )
        output = _write_run(
            run,
            output_dir=args.output_dir,
            profile_id=args.profile,
            ranker_name=ranker_name,
        )
        metrics = run["metrics"]
        print(
            f"{run['run_id']} | {run['dataset']['queries']} queries | "
            f"nDCG@10={metrics['ndcg@10']:.6f} | "
            f"MRR@10={metrics['mrr@10']:.6f} | "
            f"Success@5={metrics['success@5']:.6f}"
        )
        print(f"Run manifest: {output}")


def main() -> None:
    args = build_parser().parse_args()
    configure_logging_from_args(args)
    with logging_context(
        trace_id=new_trace_id(),
        operation="stage2_baseline",
        profile_id=args.profile,
    ):
        logger.info("evaluation_command_started", extra={"ranker_name": args.ranker})
        try:
            _execute(args)
        except Exception as exc:
            logger.error(
                "evaluation_command_failed",
                extra={
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                    "ranker_name": args.ranker,
                },
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
