"""Approval-gated strategy proposal workflow for the search Agent."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from search_quality.evaluation.artifacts import (
    atomic_write_text,
    require_clean_code_revision,
    write_immutable_json,
)
from search_quality.evaluation.baseline import (
    DEFAULT_RANDOM_SEED,
    run_candidate_baseline,
)
from search_quality.evaluation.comparison import (
    COMPARISON_EPSILON,
    compare_runs,
)
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy

logger = logging.getLogger("search_quality.agent_optimization")

PROPOSAL_SCHEMA_VERSION = "strategy-proposal-v1"
DECISION_SCHEMA_VERSION = "strategy-decision-v1"
STRATEGY_CONFIG_SCHEMA_VERSION = "search-strategy-config-v1"
DEFAULT_PROPOSAL_PROFILE = "smoke"
BASELINE_RANKER = "title-bm25"
CANDIDATE_RANKER = "title-bm25-exact-boost"
PROPOSAL_ID_PATTERN = re.compile(r"proposal-[0-9a-f]{12}\Z")
STRATEGY_CATALOG_SCHEMA_VERSION = "search-strategy-catalog-v1"


def load_strategy_catalog(*, project_root: str | Path) -> dict[str, Any]:
    """Return the currently approved runtime strategy catalog."""

    root = Path(project_root).resolve(strict=True)
    strategy_dir = _strategy_dir(root)
    catalog_path = strategy_dir / "catalog.json"
    active_path = strategy_dir / "active.json"
    catalog = _load_json_or_default(
        catalog_path,
        {"schema_version": STRATEGY_CATALOG_SCHEMA_VERSION, "strategies": []},
    )
    active = _load_json_or_default(active_path, {})
    active_strategy = active.get("strategy", {}) if isinstance(active, dict) else {}
    logger.info(
        "strategy_catalog_loaded",
        extra={
            "active_strategy_id": active_strategy.get("strategy_id"),
            "strategy_count": len(catalog.get("strategies", [])),
        },
    )
    return {
        "active": active if active else None,
        "active_strategy_id": active_strategy.get("strategy_id"),
        "schema_version": catalog.get(
            "schema_version", STRATEGY_CATALOG_SCHEMA_VERSION
        ),
        "strategies": catalog.get("strategies", []),
    }


def generate_strategy_proposal(
    *,
    project_root: str | Path,
    profile_id: Literal["smoke"] = DEFAULT_PROPOSAL_PROFILE,
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> dict[str, Any]:
    """Run one bounded optimization experiment and store a pending proposal."""

    if profile_id != DEFAULT_PROPOSAL_PROFILE:
        raise ValueError("strategy proposal workflow is currently smoke-only")
    root = Path(project_root).resolve(strict=True)
    manifest_path = root / "data" / "manifests" / "esci-stage1.json"
    policy_path = root / "configs" / "evaluation" / "esci-primary-v1.json"
    run_store = root / "runs"
    comparison_store = run_store / "comparisons"
    proposal_store = run_store / "strategy-proposals"
    revision = revision_provider(root)

    profile = EvaluationProfile.from_stage1_manifest(
        profile_id=profile_id,
        project_root=root,
        manifest_path=manifest_path,
    )
    policy = RelevancePolicy.from_path(policy_path)
    logger.info(
        "strategy_proposal_started",
        extra={"profile_id": profile_id, "candidate_ranker": CANDIDATE_RANKER},
    )
    baseline = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision=revision,
        ranker_name=BASELINE_RANKER,
        random_seed=DEFAULT_RANDOM_SEED,
    )
    candidate = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision=revision,
        ranker_name=CANDIDATE_RANKER,
        random_seed=DEFAULT_RANDOM_SEED,
    )
    comparison = compare_runs(
        baseline,
        candidate,
        comparator_revision=revision,
        expected_profile=profile_id,
        project_root=root,
        manifest_path=manifest_path,
    )

    run_store.mkdir(parents=True, exist_ok=True)
    comparison_store.mkdir(parents=True, exist_ok=True)
    proposal_store.mkdir(parents=True, exist_ok=True)
    write_immutable_json(run_store / f"{baseline['run_id']}.json", baseline)
    write_immutable_json(run_store / f"{candidate['run_id']}.json", candidate)
    write_immutable_json(
        comparison_store / f"{comparison['comparison_id']}.json", comparison
    )

    proposal_body = _build_proposal_body(
        baseline=baseline,
        candidate=candidate,
        comparison=comparison,
        profile_id=profile_id,
    )
    proposal_id = _proposal_id(proposal_body)
    proposal = {
        **proposal_body,
        "proposal_id": proposal_id,
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "status": "pending",
    }
    write_immutable_json(proposal_store / f"{proposal_id}.json", proposal)
    atomic_write_text(
        proposal_store / f"latest-{profile_id}.txt", f"{proposal_id}.json\n"
    )
    logger.info(
        "strategy_proposal_completed",
        extra={
            "baseline_run_id": baseline["run_id"],
            "candidate_run_id": candidate["run_id"],
            "comparison_id": comparison["comparison_id"],
            "profile_id": profile_id,
            "proposal_id": proposal_id,
        },
    )
    return proposal


def apply_strategy_decision(
    *,
    project_root: str | Path,
    proposal_id: str,
    decision: Literal["approve", "reject"],
) -> dict[str, Any]:
    """Record a human decision and apply an approved strategy config."""

    if not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
        raise ValueError("invalid proposal_id")
    root = Path(project_root).resolve(strict=True)
    proposal_path = root / "runs" / "strategy-proposals" / f"{proposal_id}.json"
    proposal = _load_proposal(proposal_path)
    existing_decision = _load_existing_decision(root, proposal_id)
    if existing_decision is not None:
        if existing_decision.get("decision") != decision:
            raise ValueError("proposal already has a different decision")
        return existing_decision
    if proposal["status"] != "pending":
        raise ValueError("only pending proposals can be decided")
    strategy = proposal["strategy"]
    applied = False
    active_strategy_path: str | None = None
    if decision == "approve":
        active_strategy_path = _write_strategy_catalog(root, proposal)
        applied = True
    decision_payload = {
        "active_strategy_path": active_strategy_path,
        "applied": applied,
        "decision": decision,
        "proposal_id": proposal_id,
        "schema_version": DECISION_SCHEMA_VERSION,
        "strategy_id": strategy["strategy_id"],
    }
    decision_id = _content_id("decision", decision_payload)
    decision_payload["decision_id"] = decision_id
    decision_store = root / "runs" / "strategy-decisions"
    write_immutable_json(decision_store / f"{decision_id}.json", decision_payload)
    atomic_write_text(
        _decision_pointer(root, proposal_id),
        json.dumps(decision_payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    logger.info(
        "strategy_decision_recorded",
        extra={
            "decision": decision,
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "strategy_id": strategy["strategy_id"],
        },
    )
    return decision_payload


def _build_proposal_body(
    *,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    comparison: dict[str, Any],
    profile_id: str,
) -> dict[str, Any]:
    primary_delta = comparison["aggregate_metrics"]["ndcg@10"]["delta"]
    regressions = _query_delta_examples(comparison, "regression")
    improvements = _query_delta_examples(comparison, "improvement")
    recommendation = (
        "update_strategy"
        if primary_delta > COMPARISON_EPSILON
        else "reject_strategy"
        if primary_delta < -COMPARISON_EPSILON
        else "continue_experiment"
    )
    return {
        "agent_summary": {
            "bad_case_source": "lowest baseline nDCG@10 smoke queries and comparison regressions",
            "decision_boundary": (
                "Smoke ESCI candidate-set reranking only; this does not claim "
                "full-catalog Amazon recall or production readiness."
            ),
            "recommendation": recommendation,
        },
        "baseline_run_id": baseline["run_id"],
        "candidate_run_id": candidate["run_id"],
        "comparison_id": comparison["comparison_id"],
        "evidence": {
            "aggregate_metrics": comparison["aggregate_metrics"],
            "bad_cases": _baseline_bad_cases(baseline, candidate),
            "improvements": improvements,
            "outcome_counts": comparison["outcome_counts"],
            "regressions": regressions,
        },
        "profile": profile_id,
        "strategy": {
            "catalog_entry": {
                "description": (
                    "Title BM25 plus deterministic query coverage, numeric-token "
                    "and exact-phrase boosts."
                ),
                "name": "Title BM25 Exact Boost",
                "stage": "多路召回 / 词法排序",
            },
            "config": candidate["ranker"],
            "is_new_strategy": True,
            "strategy_id": candidate["ranker"]["ranker_id"],
        },
    }


def _baseline_bad_cases(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    candidate_by_query = {
        int(item["query_id"]): item for item in candidate["per_query"]
    }
    worst = sorted(
        baseline["per_query"],
        key=lambda item: (item["metrics"]["ndcg@10"], item["query_id"]),
    )[:3]
    return [
        {
            "baseline_ndcg@10": item["metrics"]["ndcg@10"],
            "candidate_ndcg@10": candidate_by_query[int(item["query_id"])]["metrics"][
                "ndcg@10"
            ],
            "query_id": int(item["query_id"]),
            "query_text": item["query_text"],
            "top_baseline": _top_products(item),
            "top_candidate": _top_products(candidate_by_query[int(item["query_id"])]),
        }
        for item in worst
    ]


def _top_products(query: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "gain": item["gain"],
            "label": item["label"],
            "product_id": item["product_id"],
            "rank": item["rank"],
            "score": item["score"],
        }
        for item in query["ranking"][:3]
    ]


def _query_delta_examples(
    comparison: dict[str, Any], direction: Literal["improvement", "regression"]
) -> list[dict[str, Any]]:
    reverse = direction == "improvement"
    items = sorted(
        comparison["per_query"],
        key=lambda item: (
            item["metrics"]["ndcg@10"]["delta"],
            -item["query_id"] if reverse else item["query_id"],
        ),
        reverse=reverse,
    )[:3]
    if direction == "regression":
        items = [item for item in items if item["metrics"]["ndcg@10"]["delta"] < 0]
    else:
        items = [item for item in items if item["metrics"]["ndcg@10"]["delta"] > 0]
    return [
        {
            "changed_rank_count": item["changed_rank_count"],
            "ndcg@10_delta": item["metrics"]["ndcg@10"]["delta"],
            "query_id": item["query_id"],
            "query_text": item["query_text"],
            "top_10_changed": item["top_10_changed"],
        }
        for item in items
    ]


def _write_strategy_catalog(root: Path, proposal: dict[str, Any]) -> str:
    strategy_dir = _strategy_dir(root)
    strategy_dir.mkdir(parents=True, exist_ok=True)
    strategy = proposal["strategy"]
    entry = {
        "applied_from_proposal_id": proposal["proposal_id"],
        "baseline_run_id": proposal["baseline_run_id"],
        "candidate_run_id": proposal["candidate_run_id"],
        "comparison_id": proposal["comparison_id"],
        "schema_version": STRATEGY_CONFIG_SCHEMA_VERSION,
        "strategy": strategy,
    }
    strategy_id = strategy["strategy_id"]
    catalog_path = strategy_dir / "catalog.json"
    catalog = _load_json_or_default(
        catalog_path,
        {"schema_version": STRATEGY_CATALOG_SCHEMA_VERSION, "strategies": []},
    )
    strategies = [
        item
        for item in catalog.get("strategies", [])
        if item.get("strategy_id") != strategy_id
    ]
    strategies.append(
        {
            **strategy["catalog_entry"],
            "comparison_id": proposal["comparison_id"],
            "proposal_id": proposal["proposal_id"],
            "strategy_id": strategy_id,
        }
    )
    catalog = {
        "schema_version": STRATEGY_CATALOG_SCHEMA_VERSION,
        "strategies": strategies,
    }
    atomic_write_text(
        catalog_path,
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    active_path = strategy_dir / "active.json"
    atomic_write_text(
        active_path,
        json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return str(active_path.relative_to(root))


def _strategy_dir(root: Path) -> Path:
    return root / "runs" / "search-strategies"


def _decision_pointer(root: Path, proposal_id: str) -> Path:
    return root / "runs" / "strategy-decisions" / "by-proposal" / f"{proposal_id}.json"


def _load_existing_decision(root: Path, proposal_id: str) -> dict[str, Any] | None:
    pointer = _decision_pointer(root, proposal_id)
    if not pointer.exists():
        return None
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("strategy decision pointer must be a JSON object")
    if payload.get("proposal_id") != proposal_id:
        raise ValueError("strategy decision pointer does not match proposal")
    return payload


def _load_json_or_default(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("strategy catalog must be a JSON object")
    return payload


def _load_proposal(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("proposal not found")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("proposal must be a JSON object")
    if payload.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise ValueError("unsupported proposal schema")
    if payload.get("proposal_id") != path.stem:
        raise ValueError("proposal filename does not match its ID")
    return payload


def _proposal_id(payload: dict[str, Any]) -> str:
    return _content_id("proposal", payload)


def _content_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"{prefix}-{digest}"
