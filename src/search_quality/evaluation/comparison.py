"""Deterministic, compatibility-checked comparison of two evaluation Runs."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

from search_quality.evaluation.authorization import ensure_profile_authorized
from search_quality.evaluation.baseline import RUN_SCHEMA_VERSION
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.metrics import (
    ndcg_at_k,
    reciprocal_rank_at_k,
    success_at_k,
)
from search_quality.evaluation.relevance import RelevancePolicy

COMPARISON_SCHEMA_VERSION = "search-evaluation-comparison-v1"
METRIC_NAMES = ("ndcg@5", "ndcg@10", "mrr@10", "success@1", "success@5")
PRIMARY_DIAGNOSTIC_METRIC = "ndcg@10"
COMPARISON_EPSILON = 1e-12
MAX_RUN_BYTES = 16 * 1024 * 1024
_RUN_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}-[0-9a-f]{12}\Z")
_RUN_FILE_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}-[0-9a-f]{12}\.json\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_RUN_PREFIX_BY_RANKER_ID = {
    "candidate-random-v1": "random",
    "candidate-title-bm25-v1": "bm25",
    "candidate-title-keyword-overlap-v1": "overlap",
}
_RUN_KEYS = {
    "code_revision",
    "dataset",
    "evaluation_boundary",
    "metrics",
    "per_query",
    "ranker",
    "relevance_policy",
    "run_id",
    "schema_version",
}
_DATASET_KEYS = {
    "canonical_sha256",
    "eval_splits",
    "file",
    "file_sha256",
    "judgments",
    "locales",
    "origin_splits",
    "products",
    "profile",
    "queries",
    "source_commit",
    "stage1_manifest_sha256",
    "stage1_schema_version",
}
_QUERY_KEYS = {
    "candidate_count",
    "locale",
    "metrics",
    "query_id",
    "query_text",
    "ranking",
}
_RANKING_KEYS = {
    "example_id",
    "gain",
    "label",
    "locale",
    "product_id",
    "rank",
    "score",
}
logger = logging.getLogger("search_quality.evaluation")

QueryKey = tuple[str, int]
ProductKey = tuple[str, str]


def load_run(path: str | Path) -> dict[str, Any]:
    """Load one bounded JSON Run and reject duplicate keys/non-finite constants."""

    run_path = _resolve_run_path(Path(path))
    size = run_path.stat().st_size
    if size > MAX_RUN_BYTES:
        raise ValueError("Run JSON exceeds the comparison size limit")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Run JSON contains duplicate object keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("Run JSON contains a non-finite numeric constant")

    payload = json.loads(
        run_path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(payload, dict):
        raise TypeError("Run JSON root must be an object")
    return payload


def load_run_from_store(
    path: str | Path,
    *,
    store_root: str | Path,
) -> dict[str, Any]:
    """Load a Run only from one operator-trusted local artifact store."""

    requested_path = Path(path)
    configured_root = Path(store_root)
    if configured_root.is_symlink():
        raise ValueError("trusted local Run store must not be a symbolic link")
    root = configured_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("trusted local Run store must be a directory")
    if ".." in requested_path.parts:
        raise ValueError("Run store inputs must not contain parent traversal")
    if requested_path.is_symlink():
        raise ValueError("Run store inputs must not be symbolic links")
    resolved_path = requested_path.resolve(strict=True)
    if resolved_path.parent != root or not resolved_path.is_file():
        raise ValueError("Run input must be a direct file in the trusted Run store")
    actual_path = _resolve_run_path(resolved_path)
    if actual_path.is_symlink():
        raise ValueError("Run store inputs must not resolve through symbolic links")
    actual_path = actual_path.resolve(strict=True)
    if actual_path.parent != root or not actual_path.is_file():
        raise ValueError("Run input must resolve to a direct Run store file")
    run = load_run(actual_path)
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or actual_path.name != f"{run_id}.json":
        raise ValueError("Run artifact filename does not match its declared Run ID")
    return run


def _resolve_run_path(path: Path) -> Path:
    if path.suffix != ".txt":
        return path
    if path.stat().st_size > 256:
        raise ValueError("Run pointer exceeds the size limit")
    target_name = path.read_text(encoding="utf-8").strip()
    if not _RUN_FILE_PATTERN.fullmatch(target_name):
        raise ValueError("Run pointer contains an invalid same-directory target")
    target = path.parent / target_name
    if target.is_symlink():
        raise ValueError("Run pointer target must not be a symbolic link")
    return target


def compare_runs(
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
    *,
    comparator_revision: str,
    expected_profile: str,
    project_root: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Return candidate-minus-baseline aggregate and per-Query differences."""

    ensure_profile_authorized(expected_profile)
    revision = comparator_revision.strip()
    if not _SOURCE_REVISION_PATTERN.fullmatch(revision):
        raise ValueError("comparator_revision must be a full lowercase Git commit SHA")
    baseline_queries = _validate_run(baseline_run, role="baseline")
    candidate_queries = _validate_run(candidate_run, role="candidate")
    trusted_profile = EvaluationProfile.from_stage1_manifest(
        profile_id=expected_profile,
        project_root=project_root,
        manifest_path=manifest_path,
    )
    _validate_trusted_provenance(
        baseline_run, trusted_profile=trusted_profile, role="baseline"
    )
    _validate_trusted_provenance(
        candidate_run, trusted_profile=trusted_profile, role="candidate"
    )
    _validate_compatibility(
        baseline_run,
        candidate_run,
        baseline_queries=baseline_queries,
        candidate_queries=candidate_queries,
    )

    baseline_run_id = str(baseline_run["run_id"])
    candidate_run_id = str(candidate_run["run_id"])
    started = time.perf_counter()
    logger.info(
        "run_comparison_started",
        extra={
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "query_count": len(baseline_queries),
        },
    )

    aggregate_metrics = {
        metric: _metric_delta(
            _metric_value(baseline_run["metrics"], metric, "baseline aggregate"),
            _metric_value(candidate_run["metrics"], metric, "candidate aggregate"),
        )
        for metric in METRIC_NAMES
    }
    outcome_counts = {
        metric: {"improved": 0, "regressed": 0, "tied": 0} for metric in METRIC_NAMES
    }
    per_query: list[dict[str, Any]] = []

    for query_key in sorted(baseline_queries):
        baseline_query = baseline_queries[query_key]
        candidate_query = candidate_queries[query_key]
        metric_deltas: dict[str, dict[str, float]] = {}
        for metric in METRIC_NAMES:
            delta = _metric_delta(
                _metric_value(
                    baseline_query["metrics"], metric, "baseline Query metric"
                ),
                _metric_value(
                    candidate_query["metrics"], metric, "candidate Query metric"
                ),
            )
            metric_deltas[metric] = delta
            outcome_counts[metric][_delta_outcome(delta["delta"])] += 1

        baseline_by_product = _ranking_by_product(baseline_query)
        candidate_by_product = _ranking_by_product(candidate_query)
        ranking_diff = []
        for product_key, candidate_item in sorted(
            candidate_by_product.items(), key=lambda item: item[1]["rank"]
        ):
            baseline_item = baseline_by_product[product_key]
            rank_delta = int(baseline_item["rank"]) - int(candidate_item["rank"])
            ranking_diff.append(
                {
                    "baseline_rank": int(baseline_item["rank"]),
                    "baseline_score": float(baseline_item["score"]),
                    "candidate_rank": int(candidate_item["rank"]),
                    "candidate_score": float(candidate_item["score"]),
                    "example_id": int(candidate_item["example_id"]),
                    "gain": float(candidate_item["gain"]),
                    "label": str(candidate_item["label"]),
                    "locale": product_key[0],
                    "movement": (
                        "up" if rank_delta > 0 else "down" if rank_delta < 0 else "same"
                    ),
                    "product_id": product_key[1],
                    "rank_delta": rank_delta,
                }
            )

        per_query.append(
            {
                "candidate_count": int(baseline_query["candidate_count"]),
                "changed_rank_count": sum(
                    item["rank_delta"] != 0 for item in ranking_diff
                ),
                "locale": query_key[0],
                "metrics": metric_deltas,
                "query_id": query_key[1],
                "query_text": str(baseline_query["query_text"]),
                "ranking_diff": ranking_diff,
                "top_10_changed": _top_product_keys(baseline_query, 10)
                != _top_product_keys(candidate_query, 10),
            }
        )

    payload: dict[str, Any] = {
        "aggregate_metrics": aggregate_metrics,
        "baseline": _run_reference(baseline_run),
        "candidate": _run_reference(candidate_run),
        "comparator_revision": revision,
        "compatibility": {
            "dataset": copy.deepcopy(baseline_run["dataset"]),
            "evaluation_boundary": copy.deepcopy(baseline_run["evaluation_boundary"]),
            "query_count": len(per_query),
            "relevance_policy": copy.deepcopy(baseline_run["relevance_policy"]),
        },
        "comparison_epsilon": COMPARISON_EPSILON,
        "diagnostic_sort_metric": PRIMARY_DIAGNOSTIC_METRIC,
        "outcome_counts": outcome_counts,
        "per_query": per_query,
        "schema_version": COMPARISON_SCHEMA_VERSION,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    payload["comparison_id"] = (
        f"comparison-{hashlib.sha256(canonical).hexdigest()[:12]}"
    )
    logger.info(
        "run_comparison_completed",
        extra={
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "comparison_id": payload["comparison_id"],
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "query_count": len(per_query),
        },
    )
    return payload


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    """Render a deterministic human-readable summary from a comparison payload."""

    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    lines = [
        "# Search evaluation Run comparison",
        "",
        f"- Comparison: `{comparison['comparison_id']}`",
        f"- Baseline: `{baseline['run_id']}` / `{baseline['ranker_id']}`",
        f"- Candidate: `{candidate['run_id']}` / `{candidate['ranker_id']}`",
        f"- Queries: {comparison['compatibility']['query_count']}",
        "- Delta convention: candidate minus baseline; positive means improvement",
        "- Rank movement: positive rank delta means the candidate moved the product up",
        "",
        "## Aggregate metric deltas",
        "",
        "| Metric | Baseline | Candidate | Delta |",
        "|---|---:|---:|---:|",
    ]
    for metric in METRIC_NAMES:
        values = comparison["aggregate_metrics"][metric]
        lines.append(
            f"| {metric} | {values['baseline']:.6f} | "
            f"{values['candidate']:.6f} | {values['delta']:+.6f} |"
        )

    lines.extend(
        [
            "",
            "## Per-Query outcome counts",
            "",
            "| Metric | Improved | Tied | Regressed |",
            "|---|---:|---:|---:|",
        ]
    )
    for metric in METRIC_NAMES:
        counts = comparison["outcome_counts"][metric]
        lines.append(
            f"| {metric} | {counts['improved']} | {counts['tied']} | "
            f"{counts['regressed']} |"
        )

    sorted_queries = sorted(
        comparison["per_query"],
        key=lambda item: (
            item["metrics"][PRIMARY_DIAGNOSTIC_METRIC]["delta"],
            item["locale"],
            item["query_id"],
        ),
    )
    lines.extend(_query_delta_table("Largest nDCG@10 regressions", sorted_queries[:5]))
    lines.extend(
        _query_delta_table(
            "Largest nDCG@10 improvements", list(reversed(sorted_queries[-5:]))
        )
    )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "This report checks two compatible judged-candidate reranking Runs. It "
            "does not measure full-catalog Recall, and it does not automatically "
            "declare a winner. Inspect the full JSON ranking Diff before making a "
            "quality decision. Scores from different Rankers are not assumed to be "
            "on the same scale.",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_run(run: dict[str, Any], *, role: str) -> dict[QueryKey, dict[str, Any]]:
    if not isinstance(run, dict) or set(run) != _RUN_KEYS:
        raise ValueError(f"{role} Run does not match the v1 top-level schema")
    if run["schema_version"] != RUN_SCHEMA_VERSION:
        raise ValueError(f"{role} Run uses an unsupported schema version")
    run_id = run["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"{role} Run has an invalid Run ID")
    if not isinstance(run["code_revision"], str) or not (
        _SOURCE_REVISION_PATTERN.fullmatch(run["code_revision"])
    ):
        raise ValueError(f"{role} Run has an invalid code revision")

    dataset = _require_dict(run["dataset"], f"{role} dataset")
    if set(dataset) != _DATASET_KEYS:
        raise ValueError(f"{role} dataset does not match the v1 schema")
    profile = dataset.get("profile")
    if profile not in {"smoke", "dev"}:
        raise ValueError(f"{role} Run has an unsupported routine profile")
    ensure_profile_authorized(str(profile))
    if dataset.get("eval_splits") != ["dev"]:
        raise ValueError(f"{role} Run is not dev-derived")
    if dataset.get("origin_splits") != ["train"]:
        raise ValueError(f"{role} Run is not official-train-derived")
    for key in ("canonical_sha256", "file_sha256", "stage1_manifest_sha256"):
        value = dataset.get(key)
        if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(f"{role} Run has an invalid dataset hash")
    if (
        not isinstance(dataset.get("stage1_schema_version"), str)
        or not dataset["stage1_schema_version"].strip()
    ):
        raise ValueError(f"{role} Run has an invalid Stage 1 schema")
    if not isinstance(dataset.get("source_commit"), str) or not (
        _SOURCE_REVISION_PATTERN.fullmatch(dataset["source_commit"])
    ):
        raise ValueError(f"{role} Run has an invalid source revision")
    data_file = dataset.get("file")
    if (
        not isinstance(data_file, str)
        or not data_file.strip()
        or Path(data_file).name != data_file
    ):
        raise ValueError(f"{role} Run has an invalid dataset file identity")

    boundary = _require_dict(run["evaluation_boundary"], f"{role} boundary")
    if boundary != {
        "full_catalog_recall_claimed": False,
        "task": "judged-candidate-reranking",
        "unjudged_products_are_irrelevant": False,
    }:
        raise ValueError(f"{role} Run has an unsupported evaluation boundary")

    policy_payload = _require_dict(run["relevance_policy"], f"{role} policy")
    policy = RelevancePolicy.from_dict(policy_payload)
    if policy_payload != policy.to_dict():
        raise ValueError(f"{role} relevance policy is not canonical")
    ranker = _require_dict(run["ranker"], f"{role} ranker")
    if not isinstance(ranker.get("ranker_id"), str) or not ranker["ranker_id"].strip():
        raise ValueError(f"{role} Run has an invalid Ranker ID")
    expected_prefix = _RUN_PREFIX_BY_RANKER_ID.get(ranker["ranker_id"])
    if (
        expected_prefix is not None
        and run_id.rsplit("-", maxsplit=1)[0] != expected_prefix
    ):
        raise ValueError(f"{role} Run ID prefix does not match its Ranker ID")

    aggregate_metrics = _require_dict(run["metrics"], f"{role} aggregate metrics")
    _validate_metric_keys(aggregate_metrics, f"{role} aggregate metrics")
    query_items = _require_list(run["per_query"], f"{role} per_query")
    if not query_items:
        raise ValueError(f"{role} Run contains no Queries")

    queries: dict[QueryKey, dict[str, Any]] = {}
    all_products: set[ProductKey] = set()
    metric_totals = dict.fromkeys(METRIC_NAMES, 0.0)
    judgment_count = 0
    for raw_query in query_items:
        query = _require_dict(raw_query, f"{role} Query")
        if set(query) != _QUERY_KEYS:
            raise ValueError(f"{role} Query does not match the v1 schema")
        locale = _non_empty_string(query["locale"], f"{role} Query locale")
        query_id = _positive_int(query["query_id"], f"{role} Query ID")
        _non_empty_string(query["query_text"], f"{role} Query text")
        candidate_count = _positive_int(
            query["candidate_count"], f"{role} candidate count"
        )
        query_key = (locale, query_id)
        if query_key in queries:
            raise ValueError(f"{role} Run contains duplicate Query keys")

        ranking = _require_list(query["ranking"], f"{role} ranking")
        if len(ranking) != candidate_count:
            raise ValueError(f"{role} ranking length does not match candidate count")
        product_keys: set[ProductKey] = set()
        gains: list[float] = []
        relevance: list[bool] = []
        for expected_rank, raw_item in enumerate(ranking, start=1):
            item = _require_dict(raw_item, f"{role} ranking item")
            if set(item) != _RANKING_KEYS:
                raise ValueError(f"{role} ranking item does not match the v1 schema")
            item_locale = _non_empty_string(item["locale"], f"{role} product locale")
            if item_locale != locale:
                raise ValueError(f"{role} ranking crosses Query locales")
            product_id = _non_empty_string(item["product_id"], f"{role} product ID")
            product_key = (item_locale, product_id)
            if product_key in product_keys:
                raise ValueError(f"{role} ranking contains duplicate products")
            product_keys.add(product_key)
            all_products.add(product_key)
            if _positive_int(item["rank"], f"{role} rank") != expected_rank:
                raise ValueError(f"{role} ranking has non-contiguous ranks")
            _positive_int(item["example_id"], f"{role} example ID")
            score = _finite_number(item["score"], f"{role} score")
            if not math.isfinite(score):
                raise ValueError(f"{role} score is not finite")
            raw_label = _non_empty_string(item["label"], f"{role} label")
            label = raw_label.upper()
            if raw_label != label:
                raise ValueError(f"{role} label is not canonical")
            gain = _finite_number(item["gain"], f"{role} gain")
            expected_gain = policy.gain(label)
            if not math.isclose(
                gain,
                expected_gain,
                rel_tol=0.0,
                abs_tol=COMPARISON_EPSILON,
            ):
                raise ValueError(f"{role} gain does not match its relevance policy")
            gains.append(gain)
            relevance.append(policy.is_relevant(label))

        metrics = _require_dict(query["metrics"], f"{role} Query metrics")
        _validate_metric_keys(metrics, f"{role} Query metrics")
        expected_metrics = {
            "ndcg@5": ndcg_at_k(gains, candidate_gains=gains, k=5),
            "ndcg@10": ndcg_at_k(gains, candidate_gains=gains, k=10),
            "mrr@10": reciprocal_rank_at_k(relevance, 10),
            "success@1": success_at_k(relevance, 1),
            "success@5": success_at_k(relevance, 5),
        }
        for metric in METRIC_NAMES:
            observed = _metric_value(metrics, metric, f"{role} Query metrics")
            if not math.isclose(
                observed,
                expected_metrics[metric],
                rel_tol=0.0,
                abs_tol=COMPARISON_EPSILON,
            ):
                raise ValueError(f"{role} Query metric does not match its ranking")
            metric_totals[metric] += observed

        queries[query_key] = query
        judgment_count += candidate_count

    for metric in METRIC_NAMES:
        observed = _metric_value(aggregate_metrics, metric, f"{role} aggregate metrics")
        expected = metric_totals[metric] / len(queries)
        if not math.isclose(
            observed,
            expected,
            rel_tol=0.0,
            abs_tol=COMPARISON_EPSILON,
        ):
            raise ValueError(f"{role} aggregate metric does not match Query metrics")

    if dataset.get("queries") != len(queries):
        raise ValueError(f"{role} dataset Query count does not match Run contents")
    if dataset.get("judgments") != judgment_count:
        raise ValueError(f"{role} dataset judgment count does not match Run contents")
    if dataset.get("products") != len(all_products):
        raise ValueError(f"{role} dataset product count does not match Run contents")
    if dataset.get("locales") != sorted({query_key[0] for query_key in queries}):
        raise ValueError(f"{role} dataset locales do not match Run contents")

    payload_without_id = {key: value for key, value in run.items() if key != "run_id"}
    canonical = json.dumps(
        payload_without_id,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    expected_digest = hashlib.sha256(canonical).hexdigest()[:12]
    if not run_id.endswith(f"-{expected_digest}"):
        raise ValueError(f"{role} Run content does not match its Run ID")
    return queries


def _validate_trusted_provenance(
    run: dict[str, Any],
    *,
    trusted_profile: EvaluationProfile,
    role: str,
) -> None:
    dataset = run["dataset"]
    expected_identity = trusted_profile.to_manifest_dict()
    for key, expected_value in expected_identity.items():
        if dataset.get(key) != expected_value:
            raise ValueError(f"{role} Run does not match the trusted Stage 1 Manifest")
    expected_counts = {
        "judgments": trusted_profile.expected_rows,
        "products": trusted_profile.expected_products,
        "queries": trusted_profile.expected_queries,
    }
    for key, expected_value in expected_counts.items():
        if dataset.get(key) != expected_value:
            raise ValueError(f"{role} Run does not match the trusted Stage 1 Manifest")


def _validate_compatibility(
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
    *,
    baseline_queries: dict[QueryKey, dict[str, Any]],
    candidate_queries: dict[QueryKey, dict[str, Any]],
) -> None:
    for key, label in (
        ("dataset", "dataset identity"),
        ("evaluation_boundary", "evaluation boundary"),
        ("relevance_policy", "relevance policy"),
    ):
        if baseline_run[key] != candidate_run[key]:
            raise ValueError(f"Runs use different {label}")
    if set(baseline_queries) != set(candidate_queries):
        raise ValueError("Runs contain different Query sets")

    for query_key, baseline_query in baseline_queries.items():
        candidate_query = candidate_queries[query_key]
        if baseline_query["query_text"] != candidate_query["query_text"]:
            raise ValueError("Runs map one Query key to different text")
        if baseline_query["candidate_count"] != candidate_query["candidate_count"]:
            raise ValueError("Runs use different candidate counts for one Query")
        if _candidate_evidence(baseline_query) != _candidate_evidence(candidate_query):
            raise ValueError("Runs use different candidate evidence for one Query")


def _candidate_evidence(query: dict[str, Any]) -> dict[ProductKey, tuple[Any, ...]]:
    return {
        (str(item["locale"]), str(item["product_id"])): (
            int(item["example_id"]),
            str(item["label"]),
            float(item["gain"]),
        )
        for item in query["ranking"]
    }


def _ranking_by_product(query: dict[str, Any]) -> dict[ProductKey, dict[str, Any]]:
    return {
        (str(item["locale"]), str(item["product_id"])): item
        for item in query["ranking"]
    }


def _top_product_keys(query: dict[str, Any], k: int) -> list[ProductKey]:
    return [
        (str(item["locale"]), str(item["product_id"])) for item in query["ranking"][:k]
    ]


def _run_reference(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "code_revision": run["code_revision"],
        "ranker": copy.deepcopy(run["ranker"]),
        "ranker_id": run["ranker"]["ranker_id"],
        "run_id": run["run_id"],
    }


def _metric_delta(baseline: float, candidate: float) -> dict[str, float]:
    return {
        "baseline": baseline,
        "candidate": candidate,
        "delta": candidate - baseline,
    }


def _delta_outcome(delta: float) -> str:
    if delta > COMPARISON_EPSILON:
        return "improved"
    if delta < -COMPARISON_EPSILON:
        return "regressed"
    return "tied"


def _validate_metric_keys(metrics: dict[str, Any], label: str) -> None:
    if set(metrics) != set(METRIC_NAMES):
        raise ValueError(f"{label} does not define the required metrics")
    for metric in METRIC_NAMES:
        _metric_value(metrics, metric, label)


def _metric_value(metrics: dict[str, Any], metric: str, label: str) -> float:
    value = _finite_number(metrics[metric], label)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{label} must stay between zero and one")
    return value


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TypeError(f"{label} must be a positive integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _query_delta_table(title: str, queries: list[dict[str, Any]]) -> list[str]:
    lines = [
        "",
        f"## {title}",
        "",
        "| Query ID | Query | Baseline | Candidate | Delta | Changed ranks |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for query in queries:
        metric = query["metrics"][PRIMARY_DIAGNOSTIC_METRIC]
        safe_query = str(query["query_text"]).replace("\n", " ").replace("|", "\\|")
        lines.append(
            f"| {query['query_id']} | `{safe_query}` | {metric['baseline']:.6f} | "
            f"{metric['candidate']:.6f} | {metric['delta']:+.6f} | "
            f"{query['changed_rank_count']} |"
        )
    return lines
