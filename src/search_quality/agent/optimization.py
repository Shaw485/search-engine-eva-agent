"""Approval-gated strategy proposal workflow for the search Agent."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl

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
    load_run_from_store,
)
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.text import tokenize

from .contracts import RUN_ID_PATTERN
from .strategy_search import (
    TRUSTED_SMOKE_GATE_POLICY,
    BadCaseDiagnosis,
    BadCaseInput,
    CandidateSelection,
    ExactBoostCandidate,
    GatePolicy,
    StrategyEvaluation,
    WinnerSelection,
    diagnose_bad_case,
    score_strategy_comparison,
    select_exact_boost_candidates,
    select_winner,
)
from .tools import COMPARISON_ID_PATTERN, CompareRunsPayload

logger = logging.getLogger("search_quality.agent_optimization")

PROPOSAL_SCHEMA_VERSION = "strategy-proposal-v2"
DECISION_SCHEMA_VERSION = "strategy-decision-v1"
DECISION_INTENT_SCHEMA_VERSION = "strategy-decision-intent-v1"
STRATEGY_CONFIG_SCHEMA_VERSION = "search-strategy-config-v1"
DEFAULT_PROPOSAL_PROFILE = "smoke"
BASELINE_RANKER = "title-bm25"
CANDIDATE_RANKER = "title-bm25-exact-boost"
PROPOSAL_ID_PATTERN = re.compile(r"proposal-[0-9a-f]{12}\Z")
CODE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
STRATEGY_CATALOG_SCHEMA_VERSION = "search-strategy-catalog-v2"
QUERY_COMPARISON_LIMIT = 10
QUERY_RESULT_LIMIT = 10
MAX_STRATEGY_CANDIDATES = 4
MAX_STRATEGY_ARTIFACT_BYTES = 32 * 1024 * 1024
STRATEGY_DECISION_LOCK_TIMEOUT_SECONDS = 5.0
LEGACY_ACTIVE_STRATEGY_ID = "candidate-title-bm25-exact-boost-v1"
LEGACY_ACTIVE_TOP_LEVEL_FIELDS = frozenset(
    {
        "applied_from_proposal_id",
        "baseline_run_id",
        "candidate_run_id",
        "comparison_id",
        "schema_version",
        "strategy",
    }
)
LEGACY_ACTIVE_STRATEGY_FIELDS = frozenset(
    {"catalog_entry", "config", "is_new_strategy", "strategy_id"}
)
LEGACY_ACTIVE_CATALOG_ENTRY = {
    "description": (
        "Title BM25 plus deterministic query coverage, numeric-token "
        "and exact-phrase boosts."
    ),
    "name": "Title BM25 Exact Boost",
    "stage": "多路召回 / 词法排序",
}
LEGACY_ACTIVE_CONFIG = {
    "analyzer_id": "ascii-alnum-lower-v1",
    "b": 0.75,
    "coverage_boost": 0.8,
    "field": "product_title",
    "idf_scope": "per_query_judged_candidates",
    "k1": 1.5,
    "numeric_boost": 1.0,
    "phrase_boost": 1.2,
    "query_terms": "deduplicated",
    "ranker_id": LEGACY_ACTIVE_STRATEGY_ID,
    "score": "title_bm25_plus_query_coverage_numeric_and_phrase_boosts",
    "tie_break": "product_locale_product_id_ascending",
}


class ActiveStrategyChangedError(RuntimeError):
    """The active parent changed while one proposal was being evaluated."""


class StrategyProposalRejectedError(ValueError):
    """The caller requested an unsupported strategy proposal operation."""


def load_strategy_catalog(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the currently approved runtime strategy catalog."""

    root = Path(project_root).resolve(strict=True)
    run_store = _resolve_artifact_root(root, artifact_root)
    strategy_dir = _strategy_dir(run_store)
    catalog_path = strategy_dir / "catalog.json"
    catalog = _load_json_or_default(
        catalog_path,
        {"schema_version": STRATEGY_CATALOG_SCHEMA_VERSION, "strategies": []},
    )
    active = _load_active_strategy(run_store, migrate_legacy=True)
    active_strategy = active.get("strategy", {}) if isinstance(active, dict) else {}
    active_revision = _sha256_payload(active) if active else None
    history = _public_strategy_history(catalog)
    activity_logs = _public_strategy_activity_logs(history)
    logger.info(
        "strategy_catalog_loaded",
        extra={
            "active_strategy_id": active_strategy.get("strategy_id"),
            "activity_log_count": len(activity_logs),
            "history_count": len(history),
            "strategy_count": len(catalog.get("strategies", [])),
        },
    )
    return {
        "active": active if active else None,
        "active_revision": active_revision,
        "active_strategy_id": active_strategy.get("strategy_id"),
        "schema_version": catalog.get(
            "schema_version", STRATEGY_CATALOG_SCHEMA_VERSION
        ),
        "strategy_history": history,
        "strategy_activity_logs": activity_logs,
        "strategies": catalog.get("strategies", []),
    }


def generate_strategy_proposal(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    profile_id: Literal["smoke"] = DEFAULT_PROPOSAL_PROFILE,
    gate_policy: GatePolicy | None = None,
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> dict[str, Any]:
    """Run one bounded optimization experiment and store a pending proposal."""

    if profile_id != DEFAULT_PROPOSAL_PROFILE:
        raise StrategyProposalRejectedError(
            "strategy proposal workflow is currently smoke-only"
        )
    root = Path(project_root).resolve(strict=True)
    manifest_path = root / "data" / "manifests" / "esci-stage1.json"
    policy_path = root / "configs" / "evaluation" / "esci-primary-v1.json"
    run_store = _resolve_artifact_root(root, artifact_root)
    comparison_store = run_store / "comparisons"
    proposal_store = run_store / "strategy-proposals"
    revision = revision_provider(root)
    parent_active = _load_active_strategy(run_store, migrate_legacy=True)
    parent_active_strategy_id, parent_active_strategy_revision = (
        _active_strategy_identity(parent_active)
    )
    baseline_ranker_name, baseline_ranker_options = _active_baseline_ranker(
        parent_active
    )

    profile = EvaluationProfile.from_stage1_manifest(
        profile_id=profile_id,
        project_root=root,
        manifest_path=manifest_path,
    )
    policy = RelevancePolicy.from_path(policy_path)
    logger.info("strategy_proposal_started", extra={"profile_id": profile_id})
    baseline = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision=revision,
        ranker_name=baseline_ranker_name,
        ranker_options=baseline_ranker_options,
        random_seed=DEFAULT_RANDOM_SEED,
    )
    product_titles = _load_product_titles(profile.path)
    diagnoses = _diagnose_baseline(baseline, product_titles=product_titles)
    candidate_selection = select_exact_boost_candidates(
        diagnoses,
        max_candidates=MAX_STRATEGY_CANDIDATES,
    )
    if not candidate_selection.candidates:
        _require_unchanged_active_parent(
            run_store,
            parent_active_strategy_id,
            parent_active_strategy_revision,
        )
        return _persist_terminal_proposal(
            baseline=baseline,
            candidate_selection=candidate_selection,
            diagnoses=diagnoses,
            parent_active_strategy_id=parent_active_strategy_id,
            parent_active_strategy_revision=parent_active_strategy_revision,
            profile_id=profile_id,
            proposal_store=proposal_store,
            reason_code="requires_engineering",
            run_store=run_store,
        )

    experiments: list[
        tuple[
            ExactBoostCandidate,
            dict[str, Any],
            dict[str, Any],
            StrategyEvaluation,
        ]
    ] = []
    for strategy_candidate in candidate_selection.candidates:
        candidate = run_candidate_baseline(
            profile,
            policy=policy,
            code_revision=revision,
            ranker_name=CANDIDATE_RANKER,
            ranker_options=strategy_candidate.parameters.model_dump(mode="python"),
            random_seed=DEFAULT_RANDOM_SEED,
        )
        if candidate["run_id"] == baseline["run_id"]:
            logger.info(
                "strategy_candidate_skipped",
                extra={
                    "candidate_id": strategy_candidate.candidate_id,
                    "reason": "matches_active_baseline",
                },
            )
            continue
        comparison = compare_runs(
            baseline,
            candidate,
            comparator_revision=revision,
            expected_profile=profile_id,
            project_root=root,
            manifest_path=manifest_path,
        )
        evaluation = score_strategy_comparison(
            strategy_candidate,
            _comparison_payload(comparison),
            gate_policy=gate_policy or TRUSTED_SMOKE_GATE_POLICY,
        )
        experiments.append((strategy_candidate, candidate, comparison, evaluation))

    if not experiments:
        _require_unchanged_active_parent(
            run_store,
            parent_active_strategy_id,
            parent_active_strategy_revision,
        )
        return _persist_terminal_proposal(
            baseline=baseline,
            candidate_selection=candidate_selection,
            diagnoses=diagnoses,
            parent_active_strategy_id=parent_active_strategy_id,
            parent_active_strategy_revision=parent_active_strategy_revision,
            profile_id=profile_id,
            proposal_store=proposal_store,
            reason_code="strategy_space_exhausted",
            run_store=run_store,
        )

    winner_selection = select_winner([item[3] for item in experiments])
    selected = _selected_experiment(experiments, winner_selection)
    selected_strategy, candidate, comparison, selected_evaluation = selected

    _require_unchanged_active_parent(
        run_store,
        parent_active_strategy_id,
        parent_active_strategy_revision,
    )

    run_store.mkdir(parents=True, exist_ok=True)
    comparison_store.mkdir(parents=True, exist_ok=True)
    proposal_store.mkdir(parents=True, exist_ok=True)
    write_immutable_json(run_store / f"{baseline['run_id']}.json", baseline)
    for _, experiment_run, experiment_comparison, _ in experiments:
        write_immutable_json(
            run_store / f"{experiment_run['run_id']}.json", experiment_run
        )
        write_immutable_json(
            comparison_store / f"{experiment_comparison['comparison_id']}.json",
            experiment_comparison,
        )

    proposal_body = _build_proposal_body(
        baseline=baseline,
        candidate=candidate,
        candidate_selection=candidate_selection,
        comparison=comparison,
        diagnoses=diagnoses,
        evaluations=[item[3] for item in experiments],
        product_titles=product_titles,
        profile_id=profile_id,
        parent_active_strategy_id=parent_active_strategy_id,
        parent_active_strategy_revision=parent_active_strategy_revision,
        selected_evaluation=selected_evaluation,
        selected_strategy=selected_strategy,
        winner_selection=winner_selection,
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
            "query_comparison_count": len(proposal["evidence"]["query_comparisons"]),
            "strategy_candidate_count": len(experiments),
            "strategy_gate_passed": selected_evaluation.gates.passed,
        },
    )
    return proposal


def apply_strategy_decision(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    proposal_id: str,
    decision: Literal["approve", "reject"],
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> dict[str, Any]:
    """Record a human decision and apply an approved strategy config."""

    if not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
        raise ValueError("invalid proposal_id")
    root = Path(project_root).resolve(strict=True)
    run_store = _resolve_artifact_root(root, artifact_root)
    with _strategy_decision_lock(run_store):
        return _apply_strategy_decision_locked(
            root=root,
            run_store=run_store,
            proposal_id=proposal_id,
            decision=decision,
            revision_provider=revision_provider,
        )


def _apply_strategy_decision_locked(
    *,
    root: Path,
    run_store: Path,
    proposal_id: str,
    decision: Literal["approve", "reject"],
    revision_provider: Callable[[Path], str],
) -> dict[str, Any]:
    proposal_path = run_store / "strategy-proposals" / f"{proposal_id}.json"
    proposal = _load_proposal(proposal_path)
    existing_decision = _load_existing_decision(run_store, proposal_id)
    if existing_decision is not None:
        if existing_decision.get("decision") != decision:
            raise ValueError("proposal already has a different decision")
        return existing_decision
    if proposal["status"] != "pending":
        raise ValueError("only pending proposals can be decided")
    strategy = proposal["strategy"]
    if decision == "approve":
        expected_code_revision = revision_provider(root).strip()
        if not CODE_REVISION_PATTERN.fullmatch(expected_code_revision):
            raise ValueError("approval code revision must be a full Git commit SHA")
        active_strategy = _load_active_strategy(run_store)
        target_active = _strategy_active_entry(proposal)
        replaying_activation = active_strategy == target_active
        if not replaying_activation and _active_strategy_identity(active_strategy) != (
            proposal.get("parent_active_strategy_id"),
            proposal.get("parent_active_strategy_revision"),
        ):
            raise ValueError("proposal is stale relative to the active strategy")
        release_gate = proposal.get("release_gate")
        if not isinstance(release_gate, dict) or release_gate.get("passed") is not True:
            raise ValueError("only a gate-passing proposal can be approved")
        _validate_proposal_evidence(
            root,
            run_store,
            proposal,
            active_strategy=None if replaying_activation else active_strategy,
            expected_code_revision=expected_code_revision,
        )
    decision_payload = _strategy_decision_payload(proposal, decision)
    is_replay = _prepare_strategy_decision_intent(
        run_store=run_store,
        proposal=proposal,
        decision=decision,
    )
    if is_replay:
        logger.info(
            "strategy_decision_recovery_detected",
            extra={"decision": decision, "proposal_id": proposal_id},
        )
    decision_store = run_store / "strategy-decisions"
    write_immutable_json(
        decision_store / f"{decision_payload['decision_id']}.json",
        decision_payload,
    )
    if decision == "approve":
        _write_strategy_catalog(run_store, proposal, decision_payload)
    atomic_write_text(
        _decision_pointer(run_store, proposal_id),
        json.dumps(decision_payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    logger.info(
        "strategy_decision_recorded",
        extra={
            "decision": decision,
            "decision_id": decision_payload["decision_id"],
            "proposal_id": proposal_id,
            "strategy_id": strategy["strategy_id"],
        },
    )
    return decision_payload


def _persist_terminal_proposal(
    *,
    baseline: dict[str, Any],
    candidate_selection: CandidateSelection,
    diagnoses: list[BadCaseDiagnosis],
    parent_active_strategy_id: str | None,
    parent_active_strategy_revision: str | None,
    profile_id: str,
    proposal_store: Path,
    reason_code: Literal["requires_engineering", "strategy_space_exhausted"],
    run_store: Path,
) -> dict[str, Any]:
    run_store.mkdir(parents=True, exist_ok=True)
    proposal_store.mkdir(parents=True, exist_ok=True)
    write_immutable_json(run_store / f"{baseline['run_id']}.json", baseline)
    root_cause_counts = Counter(
        finding.cause for diagnosis in diagnoses for finding in diagnosis.findings
    )
    cause_order = (
        "numeric_token",
        "coverage_gap",
        "exact_phrase_displaced",
        "missing_title_signal",
    )
    target_root_cause = (
        max(
            cause_order,
            key=lambda cause: (
                root_cause_counts.get(cause, 0),
                -cause_order.index(cause),
            ),
        )
        if root_cause_counts
        else None
    )
    terminal_strategy = {
        "reason_code": reason_code,
        "strategy_family": "requires_engineering",
    }
    proposal_body = {
        "agent_summary": {
            "bad_case_source": "all smoke Queries diagnosed with deterministic title-signal rules",
            "decision_boundary": (
                "The implemented exact-boost family cannot produce a distinct, "
                "evidence-backed candidate for this diagnosis."
            ),
            "model_mode": "deterministic",
            "recommendation": "requires_engineering",
        },
        "analysis": {
            "diagnoses": [item.model_dump(mode="json") for item in diagnoses],
            "diagnosis_count": len(diagnoses),
            "root_cause_counts": {
                cause: root_cause_counts.get(cause, 0) for cause in cause_order
            },
        },
        "baseline_run_id": baseline["run_id"],
        "candidate_run_id": None,
        "comparison_id": None,
        "evidence": {
            "aggregate_metrics": {},
            "bad_cases": _baseline_only_bad_cases(baseline),
            "improvements": [],
            "outcome_counts": {},
            "query_comparisons": [],
            "regressions": [],
        },
        "experiment": {
            "candidate_selection": candidate_selection.model_dump(mode="json"),
            "evaluations": [],
            "selected_evaluation_id": None,
            "winner_selection": select_winner([]).model_dump(mode="json"),
        },
        "model_usage": {
            "calls": 0,
            "estimated_cost_usd": 0.0,
            "mode": "deterministic",
            "provider_id": None,
        },
        "parent_active_strategy_id": parent_active_strategy_id,
        "parent_active_strategy_revision": parent_active_strategy_revision,
        "profile": profile_id,
        "release_gate": {
            "checks": [],
            "passed": False,
            "policy": TRUSTED_SMOKE_GATE_POLICY.model_dump(mode="json"),
        },
        "strategy": {
            "catalog_entry": {
                "description": "当前受控 exact-boost 策略族无法处理本轮主要根因。",
                "name": "需要实现新的策略族",
                "stage": "工程候选池",
            },
            "config": terminal_strategy,
            "config_sha256": _sha256_payload(terminal_strategy),
            "explanation": {
                "expected_benefit": "避免为了产出提案而运行与根因无关的策略。",
                "mechanism": "停止当前参数搜索，并把问题转入新算法实现与代码评审。",
                "parameters": [],
                "release_conditions": ["新策略实现后必须重新运行同集 Harness"],
                "risk": "当前搜索问题仍未解决，不能把该终态当作质量提升。",
                "scoring_formula": "没有候选分数；当前策略空间不适用",
                "stage": "工程候选池",
                "support_count": len(diagnoses),
                "target_problem": (
                    "标题词法信号不足，或所有可用候选与 active 相同"
                    if target_root_cause is not None
                    else "当前规则没有发现可验证根因，不能凭空选择策略方向"
                ),
            },
            "hypothesis": "The next useful experiment requires another implemented strategy family.",
            "is_new_strategy": False,
            "strategy_id": "requires-engineering",
            "target_root_cause": target_root_cause,
        },
        "terminal_status": reason_code,
    }
    proposal_id = _proposal_id(proposal_body)
    proposal = {
        **proposal_body,
        "proposal_id": proposal_id,
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "status": "terminal",
    }
    write_immutable_json(proposal_store / f"{proposal_id}.json", proposal)
    atomic_write_text(
        proposal_store / f"latest-{profile_id}.txt", f"{proposal_id}.json\n"
    )
    logger.info(
        "strategy_proposal_completed",
        extra={
            "baseline_run_id": baseline["run_id"],
            "profile_id": profile_id,
            "proposal_id": proposal_id,
            "query_comparison_count": 0,
            "strategy_candidate_count": 0,
            "strategy_gate_passed": False,
            "terminal_status": reason_code,
        },
    )
    return proposal


def _build_proposal_body(
    *,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    candidate_selection: CandidateSelection,
    comparison: dict[str, Any],
    diagnoses: list[BadCaseDiagnosis],
    evaluations: list[StrategyEvaluation],
    product_titles: dict[tuple[str, str], str],
    profile_id: str,
    parent_active_strategy_id: str | None,
    parent_active_strategy_revision: str | None,
    selected_evaluation: StrategyEvaluation,
    selected_strategy: ExactBoostCandidate,
    winner_selection: WinnerSelection,
) -> dict[str, Any]:
    regressions = _query_delta_examples(comparison, "regression")
    improvements = _query_delta_examples(comparison, "improvement")
    recommendation = (
        "update_strategy" if selected_evaluation.gates.passed else "continue_experiment"
    )
    root_cause_counts = Counter(
        finding.cause for diagnosis in diagnoses for finding in diagnosis.findings
    )
    strategy_config = candidate["ranker"]
    config_sha256 = _sha256_payload(strategy_config)
    return {
        "agent_summary": {
            "bad_case_source": (
                "all smoke Queries diagnosed with deterministic title-signal rules; "
                "review examples are sorted by baseline quality and comparison delta"
            ),
            "decision_boundary": (
                "Smoke ESCI candidate-set reranking only; this does not claim "
                "full-catalog Amazon recall or production readiness."
            ),
            "model_mode": "deterministic",
            "recommendation": recommendation,
        },
        "analysis": {
            "diagnoses": [item.model_dump(mode="json") for item in diagnoses],
            "diagnosis_count": len(diagnoses),
            "root_cause_counts": {
                cause: root_cause_counts.get(cause, 0)
                for cause in (
                    "numeric_token",
                    "coverage_gap",
                    "exact_phrase_displaced",
                    "missing_title_signal",
                )
            },
        },
        "baseline_run_id": baseline["run_id"],
        "candidate_run_id": candidate["run_id"],
        "comparison_id": comparison["comparison_id"],
        "evidence": {
            "aggregate_metrics": comparison["aggregate_metrics"],
            "bad_cases": _baseline_bad_cases(baseline, candidate),
            "improvements": improvements,
            "outcome_counts": comparison["outcome_counts"],
            "query_comparisons": _query_comparisons(
                baseline,
                candidate,
                product_titles=product_titles,
            ),
            "regressions": regressions,
        },
        "experiment": {
            "candidate_selection": candidate_selection.model_dump(mode="json"),
            "evaluations": [_evaluation_payload(item) for item in evaluations],
            "selected_evaluation_id": selected_evaluation.evaluation_id,
            "winner_selection": winner_selection.model_dump(mode="json"),
        },
        "model_usage": {
            "calls": 0,
            "estimated_cost_usd": 0.0,
            "mode": "deterministic",
            "provider_id": None,
        },
        "parent_active_strategy_id": parent_active_strategy_id,
        "parent_active_strategy_revision": parent_active_strategy_revision,
        "profile": profile_id,
        "release_gate": selected_evaluation.gates.model_dump(mode="json"),
        "strategy": {
            "catalog_entry": _strategy_catalog_entry(selected_strategy),
            "config": strategy_config,
            "config_sha256": config_sha256,
            "explanation": _strategy_explanation(selected_strategy),
            "hypothesis": _strategy_hypothesis(selected_strategy),
            "is_new_strategy": True,
            "strategy_id": selected_strategy.candidate_id,
            "target_root_cause": selected_strategy.trigger_cause,
        },
    }


def _diagnose_baseline(
    baseline: dict[str, Any],
    *,
    product_titles: dict[tuple[str, str], str],
) -> list[BadCaseDiagnosis]:
    diagnoses: list[BadCaseDiagnosis] = []
    for query in baseline["per_query"]:
        ranking = query["ranking"]
        relevant = min(
            ranking,
            key=lambda item: (-float(item["gain"]), int(item["rank"])),
        )
        if float(relevant["gain"]) <= 0.0:
            continue
        top = ranking[0]
        relevant_title = product_titles[(relevant["locale"], relevant["product_id"])]
        top_title = product_titles[(top["locale"], top["product_id"])]
        query_terms = frozenset(tokenize(str(query["query_text"])))
        relevant_terms = frozenset(tokenize(relevant_title))
        diagnoses.append(
            diagnose_bad_case(
                BadCaseInput(
                    query_id=str(query["query_id"]),
                    query_text=str(query["query_text"]),
                    relevant_title=relevant_title,
                    baseline_top_title=top_title,
                    relevant_rank=int(relevant["rank"]),
                    title_signal_used=bool(query_terms & relevant_terms),
                )
            )
        )
    return diagnoses


def _comparison_payload(comparison: dict[str, Any]) -> CompareRunsPayload:
    comparison_epsilon = float(comparison["comparison_epsilon"])
    regressions = sorted(
        (
            item
            for item in comparison["per_query"]
            if item["metrics"]["ndcg@10"]["delta"] < -comparison_epsilon
        ),
        key=lambda item: (item["metrics"]["ndcg@10"]["delta"], item["query_id"]),
    )[:5]
    improvements = sorted(
        (
            item
            for item in comparison["per_query"]
            if item["metrics"]["ndcg@10"]["delta"] > comparison_epsilon
        ),
        key=lambda item: (
            -item["metrics"]["ndcg@10"]["delta"],
            item["query_id"],
        ),
    )[:5]
    return CompareRunsPayload.model_validate(
        {
            "aggregate_metrics": comparison["aggregate_metrics"],
            "baseline_run_id": comparison["baseline"]["run_id"],
            "candidate_run_id": comparison["candidate"]["run_id"],
            "comparison_id": comparison["comparison_id"],
            "comparison_epsilon": comparison_epsilon,
            "improvements": [_query_delta(item) for item in improvements],
            "outcome_counts": comparison["outcome_counts"],
            "query_count": comparison["compatibility"]["query_count"],
            "regressions": [_query_delta(item) for item in regressions],
        }
    )


def _query_delta(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "changed_rank_count": item["changed_rank_count"],
        "ndcg@10_delta": item["metrics"]["ndcg@10"]["delta"],
        "query_id": item["query_id"],
        "top_10_changed": item["top_10_changed"],
    }


def _selected_experiment(
    experiments: list[
        tuple[
            ExactBoostCandidate,
            dict[str, Any],
            dict[str, Any],
            StrategyEvaluation,
        ]
    ],
    winner: WinnerSelection,
) -> tuple[
    ExactBoostCandidate,
    dict[str, Any],
    dict[str, Any],
    StrategyEvaluation,
]:
    if winner.winner_evaluation_id is not None:
        return next(
            item
            for item in experiments
            if item[3].evaluation_id == winner.winner_evaluation_id
        )
    return min(
        experiments,
        key=lambda item: (
            -item[3].selection_score.total,
            -item[3].ndcg_at_10_delta,
            item[0].parameters.complexity,
            item[0].candidate_id,
        ),
    )


def _strategy_catalog_entry(candidate: ExactBoostCandidate) -> dict[str, str]:
    names = {
        "exact-conservative-v1": "保守精确匹配加权",
        "exact-numeric-v1": "型号与数字词强化",
        "exact-coverage-v1": "查询词覆盖强化",
        "exact-phrase-v1": "完整短语强化",
    }
    return {
        "description": _strategy_hypothesis(candidate),
        "name": names[candidate.candidate_id],
        "stage": "候选集词法排序",
    }


def _strategy_hypothesis(candidate: ExactBoostCandidate) -> str:
    hypotheses = {
        "exact-conservative-v1": (
            "用较小的查询词覆盖、型号数字和完整短语加权提升精确意图，"
            "同时尽量控制单个 Query 的排序退化。"
        ),
        "exact-numeric-v1": (
            "加强标题中的型号与数字词匹配，减少商品型号、版本和规格意图被泛化结果淹没。"
        ),
        "exact-coverage-v1": (
            "提高查询词覆盖比例的权重，让标题命中更多 Query 词的商品获得更高排序。"
        ),
        "exact-phrase-v1": (
            "加强完整 Query 短语匹配，让标题连续包含用户表达的商品优先展示。"
        ),
    }
    return hypotheses[candidate.candidate_id]


def _strategy_explanation(candidate: ExactBoostCandidate) -> dict[str, Any]:
    parameters = candidate.parameters
    trigger_labels = {
        "numeric_token": "型号或数字词在当前高位结果中缺失",
        "coverage_gap": "相关商品覆盖更多查询词，但排序仍然靠后",
        "exact_phrase_displaced": "包含完整查询短语的相关商品没有排到前列",
        "missing_title_signal": "标题缺少可用于排序的词法信号",
    }
    expected_benefits = {
        "exact-conservative-v1": "以较小改动验证精确匹配方向，优先降低大幅退化风险。",
        "exact-numeric-v1": "提升型号、版本、尺寸等强约束意图的首个相关结果位置。",
        "exact-coverage-v1": "提升多词 Query 的词面覆盖，减少只命中少量泛词的商品占位。",
        "exact-phrase-v1": "提升明确商品短语和长尾精确意图的排序稳定性。",
    }
    risks = {
        "exact-conservative-v1": "加权较小，可能不足以改变当前错误排序。",
        "exact-numeric-v1": "无关配件或兼容商品也可能包含相同型号，存在误提权风险。",
        "exact-coverage-v1": "标题堆叠关键词的商品可能因覆盖率高而被过度提权。",
        "exact-phrase-v1": "完整短语命中不等于商品完全相关，替代品或配件可能被误提权。",
    }
    return {
        "expected_benefit": expected_benefits[candidate.candidate_id],
        "mechanism": (
            "在标题 BM25 基础分上追加三个可解释分项，再按总分重排候选集；"
            "不改变 Query 理解、召回范围或 ESCI 标签。"
        ),
        "scoring_formula": (
            "最终得分 = 标题 BM25 基础分 + 查询词覆盖加权 × 命中的去重 Query 词占比 "
            "+ 型号数字词加权 × 命中的数字或型号词占比 "
            "+ 标题连续包含完整 Query 时的短语加权"
        ),
        "parameters": [
            {
                "key": "coverage_boost",
                "label": "查询词覆盖加权",
                "meaning": "按标题命中的去重 Query 词占比加分",
                "value": parameters.coverage_boost,
            },
            {
                "key": "numeric_boost",
                "label": "型号与数字词加权",
                "meaning": "按标题命中的数字或型号词占比加分",
                "value": parameters.numeric_boost,
            },
            {
                "key": "phrase_boost",
                "label": "完整短语加权",
                "meaning": "标题连续包含完整 Query 时追加固定分",
                "value": parameters.phrase_boost,
            },
        ],
        "risk": risks[candidate.candidate_id],
        "observation_metrics": [
            "Success@5：前 5 个结果是否至少出现一个相关商品",
            "MRR@10：第一个相关商品在 Top 10 中出现得是否足够早",
            "nDCG@10：Top 10 整体相关性及排序位置是否合理",
        ],
        "release_conditions": [
            "nDCG@10 相对基线必须提升",
            "nDCG@5 与 Success@1 不得突破下降下限",
            "退化 Query 比例不得超过发布门禁",
            "单个 Query 的最差退化幅度不得超过发布门禁",
        ],
        "stage": "候选集词法排序：标题 BM25 计分之后、Top 10 截断之前",
        "support_count": candidate.support_count,
        "target_problem": trigger_labels[candidate.trigger_cause],
    }


def _evaluation_payload(evaluation: StrategyEvaluation) -> dict[str, Any]:
    payload = evaluation.model_dump(mode="json", by_alias=True)
    payload["catalog_entry"] = _strategy_catalog_entry(evaluation.candidate)
    payload["explanation"] = _strategy_explanation(evaluation.candidate)
    return payload


def _baseline_only_bad_cases(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    worst = sorted(
        baseline["per_query"],
        key=lambda item: (item["metrics"]["ndcg@10"], item["query_id"]),
    )[:3]
    return [
        {
            "baseline_ndcg@10": item["metrics"]["ndcg@10"],
            "candidate_ndcg@10": None,
            "query_id": int(item["query_id"]),
            "query_text": item["query_text"],
            "top_baseline": _top_products(item),
            "top_candidate": [],
        }
        for item in worst
    ]


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


def _load_product_titles(path: Path) -> dict[tuple[str, str], str]:
    frame = pl.read_parquet(
        path,
        columns=["product_locale", "product_id", "product_title"],
    ).unique(subset=["product_locale", "product_id"], maintain_order=True)
    return {
        (str(row["product_locale"]), str(row["product_id"])): str(row["product_title"])
        for row in frame.iter_rows(named=True)
    }


def _query_comparisons(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    product_titles: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    candidate_by_query = {
        int(item["query_id"]): item for item in candidate["per_query"]
    }
    comparisons: list[dict[str, Any]] = []
    for baseline_query in baseline["per_query"]:
        query_id = int(baseline_query["query_id"])
        candidate_query = candidate_by_query[query_id]
        baseline_ndcg = float(baseline_query["metrics"]["ndcg@10"])
        candidate_ndcg = float(candidate_query["metrics"]["ndcg@10"])
        delta = candidate_ndcg - baseline_ndcg
        outcome = (
            "improvement"
            if delta > COMPARISON_EPSILON
            else "regression"
            if delta < -COMPARISON_EPSILON
            else "unchanged"
        )
        comparisons.append(
            {
                "baseline_ndcg@10": baseline_ndcg,
                "candidate_count": int(baseline_query["candidate_count"]),
                "candidate_ndcg@10": candidate_ndcg,
                "locale": str(baseline_query["locale"]),
                "metrics": {
                    metric: {
                        "baseline": float(baseline_query["metrics"][metric]),
                        "candidate": float(candidate_query["metrics"][metric]),
                        "delta": float(candidate_query["metrics"][metric])
                        - float(baseline_query["metrics"][metric]),
                    }
                    for metric in ("success@5", "mrr@10", "ndcg@10")
                },
                "ndcg@10_delta": delta,
                "outcome": outcome,
                "query_id": query_id,
                "query_text": str(baseline_query["query_text"]),
                "top_baseline": _display_results(
                    baseline_query,
                    product_titles=product_titles,
                ),
                "top_candidate": _display_results(
                    candidate_query,
                    product_titles=product_titles,
                ),
            }
        )
    comparisons.sort(
        key=lambda item: (
            -abs(item["ndcg@10_delta"]),
            item["baseline_ndcg@10"],
            item["query_id"],
        )
    )
    return comparisons[:QUERY_COMPARISON_LIMIT]


def _display_results(
    query: dict[str, Any],
    *,
    product_titles: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    return [
        {
            "gain": item["gain"],
            "label": item["label"],
            "locale": item["locale"],
            "product_id": item["product_id"],
            "rank": item["rank"],
            "score": item["score"],
            "title": product_titles[(item["locale"], item["product_id"])],
        }
        for item in query["ranking"][:QUERY_RESULT_LIMIT]
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


def _validate_proposal_evidence(
    project_root: Path,
    run_store: Path,
    proposal: dict[str, Any],
    *,
    active_strategy: dict[str, Any] | None,
    expected_code_revision: str,
) -> None:
    baseline_run_id = proposal.get("baseline_run_id")
    candidate_run_id = proposal.get("candidate_run_id")
    comparison_id = proposal.get("comparison_id")
    if not all(
        isinstance(value, str)
        for value in (baseline_run_id, candidate_run_id, comparison_id)
    ):
        raise ValueError("proposal evidence IDs are invalid")
    baseline = load_run_from_store(
        run_store / f"{baseline_run_id}.json",
        store_root=run_store,
    )
    candidate = load_run_from_store(
        run_store / f"{candidate_run_id}.json",
        store_root=run_store,
    )
    stored_comparison = _load_bounded_json_object(
        run_store / "comparisons" / f"{comparison_id}.json"
    )
    comparator_revision = stored_comparison.get("comparator_revision")
    if not isinstance(comparator_revision, str):
        raise ValueError("stored comparison revision is invalid")
    rebuilt_comparison = compare_runs(
        baseline,
        candidate,
        comparator_revision=comparator_revision,
        expected_profile=str(proposal.get("profile")),
        project_root=project_root,
        manifest_path=project_root / "data" / "manifests" / "esci-stage1.json",
    )
    if rebuilt_comparison != stored_comparison:
        raise ValueError("stored comparison does not match trusted Run evidence")
    if any(
        revision != expected_code_revision
        for revision in (
            baseline.get("code_revision"),
            candidate.get("code_revision"),
            comparator_revision,
        )
    ):
        raise ValueError(
            "proposal evidence code revision does not match the current deployment"
        )
    if active_strategy is not None:
        _validate_baseline_matches_active_strategy(baseline, active_strategy)
    strategy = proposal.get("strategy")
    if not isinstance(strategy, dict) or strategy.get("config") != candidate.get(
        "ranker"
    ):
        raise ValueError("proposal strategy does not match the candidate Run")
    if strategy.get("config_sha256") != _sha256_payload(strategy.get("config")):
        raise ValueError("proposal strategy config hash is invalid")
    experiment = proposal.get("experiment")
    if not isinstance(experiment, dict):
        raise ValueError("proposal experiment evidence is invalid")
    selected_evaluation_id = experiment.get("selected_evaluation_id")
    evaluations = experiment.get("evaluations")
    if not isinstance(evaluations, list):
        raise ValueError("proposal strategy evaluations are invalid")
    selected = next(
        (
            item
            for item in evaluations
            if isinstance(item, dict)
            and item.get("evaluation_id") == selected_evaluation_id
        ),
        None,
    )
    if not isinstance(selected, dict):
        raise ValueError("selected evaluation does not match proposal evidence")
    evaluation_fields = set(StrategyEvaluation.model_fields)
    if set(selected) != evaluation_fields | {"catalog_entry", "explanation"}:
        raise ValueError("selected evaluation contains unsupported fields")
    try:
        selected_evaluation = StrategyEvaluation.model_validate(
            {name: selected[name] for name in evaluation_fields}
        )
    except ValueError as exc:
        raise ValueError("selected evaluation is invalid") from exc
    if (
        selected_evaluation.comparison_id != comparison_id
        or selected_evaluation.candidate_run_id != candidate_run_id
        or selected_evaluation.baseline_run_id != baseline_run_id
        or selected_evaluation.candidate.candidate_id
        != proposal.get("strategy", {}).get("strategy_id")
        or selected_evaluation.gates.model_dump(mode="json")
        != proposal.get("release_gate")
    ):
        raise ValueError("selected evaluation does not match proposal evidence")
    candidate_ranker = candidate.get("ranker")
    if candidate_ranker != _canonical_candidate_ranker_config(
        selected_evaluation.candidate
    ):
        raise ValueError("selected strategy config does not match the candidate Run")
    if selected["catalog_entry"] != _strategy_catalog_entry(
        selected_evaluation.candidate
    ) or selected["explanation"] != _strategy_explanation(
        selected_evaluation.candidate
    ):
        raise ValueError("selected strategy display metadata is invalid")
    if selected_evaluation.gates.policy != TRUSTED_SMOKE_GATE_POLICY:
        raise ValueError("proposal does not use the trusted smoke gate policy")
    recomputed_evaluation = score_strategy_comparison(
        selected_evaluation.candidate,
        _comparison_payload(rebuilt_comparison),
        gate_policy=TRUSTED_SMOKE_GATE_POLICY,
    )
    if recomputed_evaluation != selected_evaluation:
        raise ValueError("selected evaluation does not match trusted Run evidence")


def _canonical_candidate_ranker_config(
    candidate: ExactBoostCandidate,
) -> dict[str, Any]:
    parameters = candidate.parameters
    return {
        "analyzer_id": "ascii-alnum-lower-v1",
        "b": 0.75,
        "coverage_boost": parameters.coverage_boost,
        "field": "product_title",
        "idf_scope": "per_query_judged_candidates",
        "k1": 1.5,
        "numeric_boost": parameters.numeric_boost,
        "phrase_boost": parameters.phrase_boost,
        "query_terms": "deduplicated",
        "ranker_id": "candidate-title-bm25-exact-boost-v1",
        "score": "title_bm25_plus_query_coverage_numeric_and_phrase_boosts",
        "tie_break": "product_locale_product_id_ascending",
    }


def _strategy_active_entry(proposal: dict[str, Any]) -> dict[str, Any]:
    strategy = proposal["strategy"]
    return {
        "applied_from_proposal_id": proposal["proposal_id"],
        "baseline_run_id": proposal["baseline_run_id"],
        "candidate_run_id": proposal["candidate_run_id"],
        "comparison_id": proposal["comparison_id"],
        "schema_version": STRATEGY_CONFIG_SCHEMA_VERSION,
        "strategy": strategy,
    }


def _write_strategy_catalog(
    run_store: Path,
    proposal: dict[str, Any],
    decision_payload: dict[str, Any],
) -> str:
    strategy_dir = _strategy_dir(run_store)
    strategy_dir.mkdir(parents=True, exist_ok=True)
    strategy = proposal["strategy"]
    entry = _strategy_active_entry(proposal)
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
    history = list(catalog.get("strategy_history", []))
    if not history:
        history = [
            {
                **item,
                "adopted_at": None,
                "config": {},
                "decision_id": None,
                "explanation": {},
                "metrics": {},
                "release_gate_passed": None,
                "strategy_config_sha256": None,
            }
            for item in catalog.get("strategies", [])
            if isinstance(item, dict)
        ]
    decision_id = decision_payload["decision_id"]
    if not any(item.get("decision_id") == decision_id for item in history):
        history.append(
            _strategy_history_snapshot(
                proposal=proposal,
                decision_payload=decision_payload,
            )
        )
    catalog = {
        "schema_version": STRATEGY_CATALOG_SCHEMA_VERSION,
        "strategy_history": history,
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
    # Keep the response stable and avoid exposing the server filesystem path.
    return "runs/search-strategies/active.json"


def _strategy_history_snapshot(
    *,
    proposal: dict[str, Any],
    decision_payload: dict[str, Any],
) -> dict[str, Any]:
    """Create one immutable, public-safe record for an adopted strategy."""

    strategy = proposal["strategy"]
    evidence = proposal.get("evidence")
    aggregate_metrics = evidence.get("aggregate_metrics", {}) if isinstance(
        evidence, dict
    ) else {}
    release_gate = proposal.get("release_gate")
    return {
        **strategy["catalog_entry"],
        "adopted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "comparison_id": proposal.get("comparison_id"),
        "config": strategy.get("config", {}),
        "decision_id": decision_payload["decision_id"],
        "explanation": strategy.get("explanation", {}),
        "metrics": {
            metric_id: aggregate_metrics[metric_id]
            for metric_id in ("success@5", "mrr@10", "ndcg@10")
            if metric_id in aggregate_metrics
        },
        "proposal_id": proposal["proposal_id"],
        "release_gate_passed": (
            release_gate.get("passed") if isinstance(release_gate, dict) else None
        ),
        "strategy_config_sha256": strategy["config_sha256"],
        "strategy_id": strategy["strategy_id"],
    }


def _public_strategy_history(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Return bounded strategy history without query- or result-level evidence."""

    raw_history = catalog.get("strategy_history", [])
    if not isinstance(raw_history, list):
        raise ValueError("strategy catalog history must be a list")
    if not raw_history:
        legacy_strategies = catalog.get("strategies", [])
        if not isinstance(legacy_strategies, list):
            raise ValueError("strategy catalog entries must be a list")
        raw_history = [
            {
                **item,
                "adopted_at": None,
                "config": {},
                "decision_id": None,
                "explanation": {},
                "metrics": {},
                "release_gate_passed": None,
                "strategy_config_sha256": None,
            }
            for item in legacy_strategies
            if isinstance(item, dict)
        ]
    history: list[dict[str, Any]] = []
    for item in raw_history[-100:]:
        if not isinstance(item, dict):
            raise ValueError("strategy catalog history entry must be an object")
        history.append(
            {
                key: item.get(key)
                for key in (
                    "adopted_at",
                    "comparison_id",
                    "config",
                    "decision_id",
                    "description",
                    "explanation",
                    "metrics",
                    "name",
                    "proposal_id",
                    "release_gate_passed",
                    "stage",
                    "strategy_config_sha256",
                    "strategy_id",
                )
            }
        )
    return list(reversed(history))


def _public_strategy_activity_logs(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive a compact lifecycle audit log from approved history snapshots."""

    return [
        {
            "event_id": (
                f"activity-{item.get('decision_id') or item.get('proposal_id') or item.get('strategy_id')}"
            ),
            "event_type": "strategy_approved_and_activated",
            "message": "站长批准策略，配置已写入运行目录并成为当时的生效版本。",
            "occurred_at": item.get("adopted_at"),
            "proposal_id": item.get("proposal_id"),
            "decision_id": item.get("decision_id"),
            "strategy_id": item.get("strategy_id"),
            "strategy_name": item.get("name"),
        }
        for item in history
    ]


def _resolve_artifact_root(
    project_root: Path, artifact_root: str | Path | None
) -> Path:
    if artifact_root is None:
        return project_root / "runs"
    requested = Path(artifact_root)
    if not requested.is_absolute():
        raise ValueError("strategy artifact root must be an absolute path")
    resolved = requested.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("strategy artifact root must be a directory")
    return resolved


def _strategy_dir(run_store: Path) -> Path:
    return run_store / "search-strategies"


def _load_active_strategy(
    run_store: Path,
    *,
    migrate_legacy: bool = False,
) -> dict[str, Any]:
    active_path = _strategy_dir(run_store) / "active.json"
    active = _load_json_or_default(active_path, {})
    if migrate_legacy and active:
        with _strategy_decision_lock(run_store):
            current = _load_json_or_default(active_path, {})
            if current:
                return _migrate_legacy_active_strategy(active_path, current)
            return current
    return active


def _migrate_legacy_active_strategy(
    active_path: Path,
    active: dict[str, Any],
) -> dict[str, Any]:
    """Add the missing config hash to the one supported v1 active artifact."""

    strategy = active.get("strategy")
    if not isinstance(strategy, dict) or "config_sha256" in strategy:
        return active
    if not _is_supported_legacy_active_strategy(active):
        raise ValueError(
            "active strategy config hash is missing and the artifact is not a "
            "supported legacy v1 strategy"
        )
    migrated_strategy = {
        **strategy,
        "config_sha256": _sha256_payload(strategy["config"]),
    }
    migrated = {**active, "strategy": migrated_strategy}
    atomic_write_text(
        active_path,
        json.dumps(migrated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    logger.info(
        "legacy_active_strategy_migrated",
        extra={
            "schema_version": active["schema_version"],
            "strategy_id": strategy["strategy_id"],
        },
    )
    return migrated


def _is_supported_legacy_active_strategy(active: dict[str, Any]) -> bool:
    if set(active) != LEGACY_ACTIVE_TOP_LEVEL_FIELDS:
        return False
    strategy = active.get("strategy")
    if not isinstance(strategy, dict) or set(strategy) != LEGACY_ACTIVE_STRATEGY_FIELDS:
        return False
    if (
        active.get("schema_version") != STRATEGY_CONFIG_SCHEMA_VERSION
        or strategy.get("strategy_id") != LEGACY_ACTIVE_STRATEGY_ID
        or strategy.get("is_new_strategy") is not True
        or strategy.get("catalog_entry") != LEGACY_ACTIVE_CATALOG_ENTRY
        or strategy.get("config") != LEGACY_ACTIVE_CONFIG
    ):
        return False
    evidence_patterns = (
        (active.get("applied_from_proposal_id"), PROPOSAL_ID_PATTERN.pattern),
        (active.get("baseline_run_id"), rf"(?:{RUN_ID_PATTERN})\Z"),
        (active.get("candidate_run_id"), rf"(?:{RUN_ID_PATTERN})\Z"),
        (active.get("comparison_id"), rf"(?:{COMPARISON_ID_PATTERN})\Z"),
    )
    return all(
        isinstance(value, str) and re.fullmatch(pattern, value)
        for value, pattern in evidence_patterns
    )


def _active_strategy_identity(
    active: dict[str, Any],
) -> tuple[str | None, str | None]:
    if not active:
        return None, None
    strategy = active.get("strategy") if isinstance(active, dict) else None
    strategy_id = strategy.get("strategy_id") if isinstance(strategy, dict) else None
    if strategy_id is not None and not isinstance(strategy_id, str):
        raise ValueError("active strategy ID must be a string")
    if strategy_id is None:
        raise ValueError("active strategy must contain a strategy ID")
    return strategy_id, _sha256_payload(active)


def _active_strategy_snapshot(run_store: Path) -> tuple[str | None, str | None]:
    return _active_strategy_identity(_load_active_strategy(run_store))


def _require_unchanged_active_parent(
    run_store: Path,
    expected_strategy_id: str | None,
    expected_revision: str | None,
) -> None:
    if _active_strategy_snapshot(run_store) != (
        expected_strategy_id,
        expected_revision,
    ):
        raise ActiveStrategyChangedError(
            "active strategy changed during proposal generation"
        )


def _active_baseline_ranker(
    active: dict[str, Any],
) -> tuple[str, dict[str, float]]:
    if not active:
        return BASELINE_RANKER, {}
    config = _trusted_active_ranker_config(active)
    if config.get("ranker_id") != "candidate-title-bm25-exact-boost-v1":
        raise ValueError("active strategy Ranker is not supported by the optimizer")
    option_names = ("b", "coverage_boost", "k1", "numeric_boost", "phrase_boost")
    options = {name: config.get(name) for name in option_names}
    if any(type(value) not in (int, float) for value in options.values()):
        raise ValueError("active strategy Ranker options are invalid")
    return CANDIDATE_RANKER, {
        name: float(value) for name, value in options.items() if value is not None
    }


def _trusted_active_ranker_config(active: dict[str, Any]) -> dict[str, Any]:
    strategy = active.get("strategy")
    config = strategy.get("config") if isinstance(strategy, dict) else None
    if not isinstance(config, dict):
        raise ValueError("active strategy config is invalid")
    if strategy.get("config_sha256") != _sha256_payload(config):
        raise ValueError("active strategy config hash is invalid")
    return config


def _validate_baseline_matches_active_strategy(
    baseline: dict[str, Any],
    active: dict[str, Any],
) -> None:
    baseline_ranker = baseline.get("ranker")
    if not isinstance(baseline_ranker, dict):
        raise ValueError("proposal baseline Ranker config is invalid")
    if active:
        expected_ranker = _trusted_active_ranker_config(active)
    else:
        expected_ranker = {
            "analyzer_id": "ascii-alnum-lower-v1",
            "b": 0.75,
            "field": "product_title",
            "idf_scope": "per_query_judged_candidates",
            "k1": 1.5,
            "query_terms": "deduplicated",
            "ranker_id": "candidate-title-bm25-v1",
            "tie_break": "product_locale_product_id_ascending",
        }
    if baseline_ranker != expected_ranker:
        raise ValueError("proposal baseline does not match the active strategy")


@contextmanager
def _strategy_decision_lock(run_store: Path) -> Iterator[None]:
    decision_store = run_store / "strategy-decisions"
    decision_store.mkdir(parents=True, exist_ok=True)
    lock_path = decision_store / ".decision.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif lock_path.is_symlink():
        raise ValueError("strategy decision lock must not be a symbolic link")
    file_descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        deadline = time.monotonic() + STRATEGY_DECISION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("strategy decision lock timed out") from exc
                time.sleep(0.05)
        yield
    finally:
        if locked:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        os.close(file_descriptor)


def _decision_pointer(run_store: Path, proposal_id: str) -> Path:
    return run_store / "strategy-decisions" / "by-proposal" / f"{proposal_id}.json"


def _decision_intent_path(run_store: Path, proposal_id: str) -> Path:
    return run_store / "strategy-decisions" / "intents" / f"{proposal_id}.json"


def _strategy_decision_intent(
    proposal: dict[str, Any], decision: Literal["approve", "reject"]
) -> dict[str, Any]:
    strategy = proposal["strategy"]
    return {
        "decision": decision,
        "parent_active_strategy_id": proposal.get("parent_active_strategy_id"),
        "parent_active_strategy_revision": proposal.get(
            "parent_active_strategy_revision"
        ),
        "proposal_id": proposal["proposal_id"],
        "schema_version": DECISION_INTENT_SCHEMA_VERSION,
        "strategy_config_sha256": strategy["config_sha256"],
        "strategy_id": strategy["strategy_id"],
    }


def _prepare_strategy_decision_intent(
    *,
    run_store: Path,
    proposal: dict[str, Any],
    decision: Literal["approve", "reject"],
) -> bool:
    expected = _strategy_decision_intent(proposal, decision)
    intent_path = _decision_intent_path(run_store, proposal["proposal_id"])
    intent_exists = intent_path.exists() or intent_path.is_symlink()
    if not intent_exists:
        write_immutable_json(intent_path, expected)
        return False
    existing = _load_bounded_json_object(intent_path)
    if existing.get("decision") != decision:
        raise ValueError("proposal already has a different decision")
    if existing != expected:
        raise ValueError("strategy decision intent does not match proposal")
    return True


def _strategy_decision_payload(
    proposal: dict[str, Any], decision: Literal["approve", "reject"]
) -> dict[str, Any]:
    strategy = proposal["strategy"]
    payload = {
        "active_strategy_path": (
            "runs/search-strategies/active.json" if decision == "approve" else None
        ),
        "applied": decision == "approve",
        "decision": decision,
        "proposal_id": proposal["proposal_id"],
        "schema_version": DECISION_SCHEMA_VERSION,
        "strategy_config_sha256": strategy["config_sha256"],
        "strategy_id": strategy["strategy_id"],
    }
    payload["decision_id"] = _content_id("decision", payload)
    return payload


def _load_existing_decision(run_store: Path, proposal_id: str) -> dict[str, Any] | None:
    pointer = _decision_pointer(run_store, proposal_id)
    if not pointer.exists():
        return None
    payload = _load_bounded_json_object(pointer)
    if payload.get("proposal_id") != proposal_id:
        raise ValueError("strategy decision pointer does not match proposal")
    return payload


def _load_json_or_default(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("strategy state artifact must not be a symbolic link")
    if not path.exists():
        return default
    return _load_bounded_json_object(path)


def _load_bounded_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("strategy evidence artifact not found")
    if path.stat().st_size > MAX_STRATEGY_ARTIFACT_BYTES:
        raise ValueError("strategy evidence artifact exceeds the size limit")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("strategy evidence contains duplicate object keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("strategy evidence contains a non-finite number")

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("strategy evidence must be a JSON object")
    return payload


def _load_proposal(path: Path) -> dict[str, Any]:
    payload = _load_bounded_json_object(path)
    if payload.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise ValueError("unsupported proposal schema")
    if payload.get("proposal_id") != path.stem:
        raise ValueError("proposal filename does not match its ID")
    proposal_body = {
        key: value
        for key, value in payload.items()
        if key not in {"proposal_id", "schema_version", "status"}
    }
    if _proposal_id(proposal_body) != payload["proposal_id"]:
        raise ValueError("proposal content does not match its ID")
    return payload


def _proposal_id(payload: dict[str, Any]) -> str:
    return _content_id("proposal", payload)


def _content_id(prefix: str, payload: dict[str, Any]) -> str:
    return f"{prefix}-{_sha256_payload(payload)[:12]}"


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
