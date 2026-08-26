"""Run the Stage 2 title-BM25 baseline on a safe development profile."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from search_quality.evaluation.baseline import run_candidate_title_bm25_baseline
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("smoke", "dev"), default="smoke")
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


def main() -> None:
    args = build_parser().parse_args()
    policy = RelevancePolicy.from_path(args.policy)
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id=args.profile,
        project_root=PROJECT_ROOT,
        manifest_path=args.manifest,
    )
    run = run_candidate_title_bm25_baseline(
        profile,
        policy=policy,
        code_revision=_code_revision(),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"{run['run_id']}.json"
    serialized = json.dumps(run, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output.is_file() and output.read_text(encoding="utf-8") != serialized:
        raise RuntimeError(f"immutable Run ID collision at {output}")
    if not output.is_file():
        output.write_text(serialized, encoding="utf-8")
    latest = args.output_dir / f"latest-{args.profile}.txt"
    latest.write_text(output.name + "\n", encoding="utf-8")
    metrics = run["metrics"]
    print(
        f"{run['run_id']} | {run['dataset']['queries']} queries | "
        f"nDCG@10={metrics['ndcg@10']:.6f} | "
        f"MRR@10={metrics['mrr@10']:.6f} | "
        f"Success@5={metrics['success@5']:.6f}"
    )
    print(f"Run manifest: {output}")


if __name__ == "__main__":
    main()
