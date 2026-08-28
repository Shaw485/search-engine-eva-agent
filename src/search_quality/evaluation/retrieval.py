"""Query-scoped judged-pool retrieval and stage-retention Harness."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import polars as pl

from search_quality.evaluation.authorization import ensure_profile_authorized
from search_quality.evaluation.baseline import validate_evaluation_frame
from search_quality.evaluation.datasets import EvaluationProfile, sha256_file
from search_quality.evaluation.metrics import (
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
)
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.ranking.base import ProductKey
from search_quality.retrieval import QueryScopedSearchPipeline, RetrievalDocument

RUN_SCHEMA_VERSION = "query-scoped-retrieval-run-v1"
PROFILE_SCHEMA_VERSION = "query-scoped-retrieval-profile-v0"
DEFAULT_PROFILE_CONFIG = Path("configs/evaluation/query-scoped-retrieval-smoke-v0.json")
CODE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
logger = logging.getLogger("search_quality.retrieval")


def _normalized_frame(path: Path) -> pl.DataFrame:
    return pl.read_parquet(path).with_columns(
        pl.col("query_text").cast(pl.String).str.strip_chars(),
        pl.col("product_id").cast(pl.String).str.strip_chars(),
        pl.col("product_title").cast(pl.String).str.strip_chars(),
        pl.col("esci_label").cast(pl.String).str.to_uppercase(),
        pl.col("product_locale").cast(pl.String).str.to_lowercase(),
        pl.col("eval_split").cast(pl.String).str.to_lowercase(),
        pl.col("origin_split").cast(pl.String).str.to_lowercase(),
    )


def _load_profile_contract(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("retrieval profile must contain an object")
    expected_fields = {
        "eligible_metrics",
        "forbidden_claims",
        "judged_pairs",
        "policy_file_sha256",
        "pool_construction",
        "possible_pairs_in_scope",
        "profile",
        "query_count",
        "query_keys_sha256",
        "schema_version",
        "source_file_sha256",
        "stage1_manifest_sha256",
        "unknown_pairs_in_scope",
        "unjudged_treatment",
    }
    if set(payload) != expected_fields:
        missing = sorted(expected_fields - set(payload))
        extra = sorted(set(payload) - expected_fields)
        raise ValueError(
            f"retrieval profile fields do not match; missing={missing}, extra={extra}"
        )
    if payload["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported retrieval profile schema")
    if payload["profile"] != "smoke":
        raise ValueError("query-scoped retrieval v0 is smoke-only")
    if payload["pool_construction"] != "per_query_fully_judged_candidate_pool":
        raise ValueError("retrieval profile must use per-Query fully judged pools")
    if payload["unjudged_treatment"] != "exclude_out_of_scope":
        raise ValueError("unjudged cross-Query products must remain out of scope")
    if payload["unknown_pairs_in_scope"] != 0:
        raise ValueError("formal judged Recall requires zero unknown in-scope pairs")
    if payload["possible_pairs_in_scope"] != payload["judged_pairs"]:
        raise ValueError("formal judged Recall requires complete in-scope judgments")
    return payload


def _query_keys_sha256(frame: pl.DataFrame) -> str:
    keys = sorted(
        [str(row["product_locale"]), int(row["query_id"])]
        for row in frame.select("product_locale", "query_id")
        .unique()
        .iter_rows(named=True)
    )
    canonical = json.dumps(keys, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("aggregate metric requires at least one Query")
    return math.fsum(values) / len(values)


def _ranking_metrics(
    keys: Sequence[ProductKey],
    *,
    labels_by_key: Mapping[ProductKey, str],
    candidate_gains: Sequence[float],
    total_relevant: int,
    policy: RelevancePolicy,
) -> dict[str, float]:
    if len(keys) != len(set(keys)):
        raise ValueError("stage ranking contains duplicate products")
    unknown = sorted(set(keys) - set(labels_by_key))
    if unknown:
        raise ValueError(f"stage ranking contains unknown products: {unknown[:3]}")
    labels = [labels_by_key[key] for key in keys]
    relevant = [policy.is_relevant(label) for label in labels]
    gains = [policy.gain(label) for label in labels]
    return {
        "judged_recall@5": recall_at_k(
            relevant,
            total_relevant=total_relevant,
            k=5,
        ),
        "judged_recall@10": recall_at_k(
            relevant,
            total_relevant=total_relevant,
            k=10,
        ),
        "mrr@10": reciprocal_rank_at_k(relevant, 10),
        "ndcg@10": ndcg_at_k(gains, candidate_gains=candidate_gains, k=10),
    }


def _ranked_evidence(
    hits,
    *,
    labels_by_key: Mapping[ProductKey, str],
    policy: RelevancePolicy,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for hit in hits:
        label = labels_by_key[hit.key]
        item = hit.to_dict()
        item.update({"gain": policy.gain(label), "label": label})
        evidence.append(item)
    return evidence


def _lineage(
    *,
    labels_by_key: Mapping[ProductKey, str],
    policy: RelevancePolicy,
    pipeline_result,
) -> list[dict[str, Any]]:
    route_ranks = {
        channel.channel_id: {hit.key: hit.rank for hit in channel.hits}
        for channel in pipeline_result.channels
    }
    union = set(pipeline_result.recall_union)
    fused_ranks = {hit.key: hit.rank for hit in pipeline_result.fused_hits}
    coarse_ranks = {hit.key: hit.rank for hit in pipeline_result.coarse_hits}
    rows: list[dict[str, Any]] = []
    for key in sorted(
        key for key, label in labels_by_key.items() if policy.is_relevant(label)
    ):
        if key not in union:
            first_loss_stage = "recall"
        elif key not in fused_ranks:
            first_loss_stage = "fusion"
        elif key not in coarse_ranks:
            first_loss_stage = "coarse_rank"
        else:
            first_loss_stage = "retained"
        label = labels_by_key[key]
        rows.append(
            {
                "coarse_rank": coarse_ranks.get(key),
                "first_loss_stage": first_loss_stage,
                "fused_rank": fused_ranks.get(key),
                "gain": policy.gain(label),
                "label": label,
                "locale": key[0],
                "product_id": key[1],
                "route_ranks": {
                    channel_id: ranks.get(key)
                    for channel_id, ranks in sorted(route_ranks.items())
                },
            }
        )
    return rows


def run_query_scoped_retrieval(
    profile: EvaluationProfile,
    *,
    policy: RelevancePolicy,
    policy_path: str | Path,
    project_root: str | Path,
    code_revision: str,
    profile_config_path: str | Path | None = None,
    pipeline_variant: str = "title-exact-v1",
) -> dict[str, Any]:
    """Evaluate stage retention inside each Query's fully judged ESCI pool.

    This is deliberately not a shared-corpus or full-catalog Recall benchmark.
    """

    # Authorization precedes data-path existence checks and data reads.
    ensure_profile_authorized(profile.profile_id)
    root = Path(project_root).resolve(strict=True)
    revision = code_revision.strip()
    if not CODE_REVISION_PATTERN.fullmatch(revision):
        raise ValueError("code_revision must be a full lowercase Git commit SHA")
    if profile.profile_id != "smoke":
        raise ValueError("query-scoped retrieval v0 is smoke-only")
    contract_path = Path(profile_config_path or root / DEFAULT_PROFILE_CONFIG)
    contract = _load_profile_contract(contract_path)
    policy_file = Path(policy_path)

    if not profile.path.is_file():
        raise FileNotFoundError(profile.path)
    observed_source_sha = sha256_file(profile.path)
    if observed_source_sha != profile.file_sha256:
        raise ValueError("source data does not match its Stage 1 manifest")
    if observed_source_sha != contract["source_file_sha256"]:
        raise ValueError("source data does not match the retrieval profile")
    if profile.stage1_manifest_sha256 != contract["stage1_manifest_sha256"]:
        raise ValueError("Stage 1 manifest does not match the retrieval profile")
    if sha256_file(policy_file) != contract["policy_file_sha256"]:
        raise ValueError("relevance policy does not match the retrieval profile")

    frame = _normalized_frame(profile.path)
    validate_evaluation_frame(frame)
    if not bool(frame.get_column("is_smoke").all()):
        raise ValueError("smoke retrieval profile contains non-smoke rows")
    query_count = frame.select(
        pl.struct("product_locale", "query_id").n_unique()
    ).item()
    if frame.height != contract["judged_pairs"]:
        raise ValueError("judged pair count does not match retrieval profile")
    if query_count != contract["query_count"]:
        raise ValueError("Query count does not match retrieval profile")
    if _query_keys_sha256(frame) != contract["query_keys_sha256"]:
        raise ValueError("Query keys do not match retrieval profile")

    started = time.perf_counter()
    logger.info(
        "retrieval_run_started",
        extra={
            "judgment_count": frame.height,
            "profile_id": profile.profile_id,
            "query_count": query_count,
        },
    )
    per_query: list[dict[str, Any]] = []
    aggregate_values: dict[str, dict[str, list[float]]] = {}
    exact_unique_relevant_count = 0
    unique_relevant_by_channel: Counter[str] = Counter()
    loss_counts: Counter[str] = Counter()
    zero_result_queries = 0
    pipeline_config: dict[str, Any] | None = None
    pipeline_id: str | None = None

    for query_frame in frame.sort(
        "product_locale", "query_id", "product_id"
    ).partition_by("product_locale", "query_id", maintain_order=True):
        query_started = time.perf_counter()
        locale = str(query_frame.item(0, "product_locale"))
        query_id = int(query_frame.item(0, "query_id"))
        query_text = str(query_frame.item(0, "query_text"))
        documents = [
            RetrievalDocument(
                brand=str(row["product_brand"] or ""),
                bullet_point=str(row["product_bullet_point"] or ""),
                description=str(row["product_description"] or ""),
                locale=str(row["product_locale"]),
                product_id=str(row["product_id"]),
                title=str(row["product_title"]),
            )
            for row in query_frame.select(
                "product_locale",
                "product_id",
                "product_title",
                "product_brand",
                "product_bullet_point",
                "product_description",
            ).iter_rows(named=True)
        ]
        labels_by_key = {
            (str(row["product_locale"]), str(row["product_id"])): str(row["esci_label"])
            for row in query_frame.select(
                "product_locale", "product_id", "esci_label"
            ).iter_rows(named=True)
        }
        documents_by_key = {document.key: document for document in documents}
        total_relevant = sum(
            policy.is_relevant(label) for label in labels_by_key.values()
        )
        if total_relevant < 1:
            raise ValueError("every formal retrieval Query needs a relevant judgment")
        candidate_gains = [policy.gain(label) for label in labels_by_key.values()]
        pipeline = QueryScopedSearchPipeline(documents, variant=pipeline_variant)
        if pipeline_config is None:
            pipeline_config = dict(pipeline.config)
            pipeline_id = pipeline.pipeline_id
        elif pipeline.config != pipeline_config or pipeline.pipeline_id != pipeline_id:
            raise ValueError("pipeline configuration changed within one Run")
        pipeline_result = pipeline.run(query_text)
        channel_metrics = {
            channel.channel_id: _ranking_metrics(
                [hit.key for hit in channel.hits],
                labels_by_key=labels_by_key,
                candidate_gains=candidate_gains,
                total_relevant=total_relevant,
                policy=policy,
            )
            for channel in pipeline_result.channels
        }
        fusion_metrics = _ranking_metrics(
            [hit.key for hit in pipeline_result.fused_hits],
            labels_by_key=labels_by_key,
            candidate_gains=candidate_gains,
            total_relevant=total_relevant,
            policy=policy,
        )
        coarse_metrics = _ranking_metrics(
            [hit.key for hit in pipeline_result.coarse_hits],
            labels_by_key=labels_by_key,
            candidate_gains=candidate_gains,
            total_relevant=total_relevant,
            policy=policy,
        )
        union_relevant = sum(
            policy.is_relevant(labels_by_key[key])
            for key in pipeline_result.recall_union
        )
        union_coverage = union_relevant / total_relevant
        lineage = _lineage(
            labels_by_key=labels_by_key,
            policy=policy,
            pipeline_result=pipeline_result,
        )
        loss_counts.update(item["first_loss_stage"] for item in lineage)

        channel_keys = {
            channel.channel_id: {hit.key for hit in channel.hits}
            for channel in pipeline_result.channels
        }
        exact_keys = channel_keys["exact-title-recall-v1"]
        bm25_keys = channel_keys["title-bm25-recall-v1"]
        exact_unique_relevant_count += sum(
            key in exact_keys and key not in bm25_keys and policy.is_relevant(label)
            for key, label in labels_by_key.items()
        )
        for channel_id, keys in channel_keys.items():
            other_keys = set().union(
                *(
                    items
                    for other_id, items in channel_keys.items()
                    if other_id != channel_id
                )
            )
            unique_relevant_by_channel[channel_id] += sum(
                key in keys and key not in other_keys and policy.is_relevant(label)
                for key, label in labels_by_key.items()
            )
        if not pipeline_result.fused_hits:
            zero_result_queries += 1

        stage_metrics = {
            "coarse-title-bm25-v1": coarse_metrics,
            "rrf-v1": fusion_metrics,
            **channel_metrics,
        }
        for stage_id, metrics in stage_metrics.items():
            aggregate_values.setdefault(stage_id, {})
            for metric_name, value in metrics.items():
                aggregate_values[stage_id].setdefault(metric_name, []).append(value)
        aggregate_values.setdefault("recall-union-v1", {}).setdefault(
            "judged_relevant_coverage", []
        ).append(union_coverage)

        per_query.append(
            {
                "judgments": [
                    {
                        "gain": policy.gain(labels_by_key[key]),
                        "label": labels_by_key[key],
                        "locale": key[0],
                        "product_brand": documents_by_key[key].brand,
                        "product_bullet_point": documents_by_key[key].bullet_point,
                        "product_description": documents_by_key[key].description,
                        "product_id": key[1],
                        "product_title": documents_by_key[key].title,
                    }
                    for key in sorted(labels_by_key)
                ],
                "lineage": lineage,
                "locale": locale,
                "metrics": {
                    "coarse_rank": coarse_metrics,
                    "fusion": fusion_metrics,
                    "recall_channels": channel_metrics,
                    "recall_union": {
                        "judged_relevant_coverage": union_coverage,
                    },
                },
                "pool_count": len(documents),
                "query_id": query_id,
                "query_text": query_text,
                "rankings": {
                    "coarse_rank": _ranked_evidence(
                        pipeline_result.coarse_hits,
                        labels_by_key=labels_by_key,
                        policy=policy,
                    ),
                    "fusion": _ranked_evidence(
                        pipeline_result.fused_hits,
                        labels_by_key=labels_by_key,
                        policy=policy,
                    ),
                    "recall_channels": {
                        channel.channel_id: _ranked_evidence(
                            channel.hits,
                            labels_by_key=labels_by_key,
                            policy=policy,
                        )
                        for channel in pipeline_result.channels
                    },
                },
                "relevant_count": total_relevant,
                "stage_counts": {
                    "coarse_output": len(pipeline_result.coarse_hits),
                    "fusion_output": len(pipeline_result.fused_hits),
                    "recall_union": len(pipeline_result.recall_union),
                },
            }
        )
        logger.debug(
            "retrieval_query_completed",
            extra={
                "coarse_result_count": len(pipeline_result.coarse_hits),
                "duration_ms": round(
                    (time.perf_counter() - query_started) * 1000,
                    3,
                ),
                "fusion_result_count": len(pipeline_result.fused_hits),
                "pool_count": len(documents),
                "profile_id": profile.profile_id,
                "query_id": query_id,
                "relevant_count": total_relevant,
            },
        )

    if pipeline_config is None or pipeline_id is None:
        raise RuntimeError("retrieval Run did not evaluate any Query")
    aggregate_stages = {
        stage_id: {
            f"mean_{metric_name}": _mean(values)
            for metric_name, values in metrics.items()
        }
        for stage_id, metrics in sorted(aggregate_values.items())
    }
    payload: dict[str, Any] = {
        "aggregate": {
            "exact_unique_relevant_count": exact_unique_relevant_count,
            "first_loss_counts": {
                stage: loss_counts.get(stage, 0)
                for stage in ("recall", "fusion", "coarse_rank", "retained")
            },
            "stages": aggregate_stages,
            "unique_relevant_by_channel": {
                channel["channel_id"]: unique_relevant_by_channel.get(
                    channel["channel_id"], 0
                )
                for channel in pipeline_config["channels"]
            },
            "zero_result_queries": zero_result_queries,
        },
        "code_revision": revision,
        "dataset": {
            **profile.to_manifest_dict(),
            "judged_pairs": frame.height,
            "possible_pairs_in_scope": contract["possible_pairs_in_scope"],
            "query_count": query_count,
            "query_keys_sha256": contract["query_keys_sha256"],
            "unknown_pairs_in_scope": 0,
        },
        "evaluation_boundary": {
            "denominator_complete": True,
            "eligible_metrics": contract["eligible_metrics"],
            "forbidden_claims": contract["forbidden_claims"],
            "full_catalog_recall_claimed": False,
            "pool_construction": contract["pool_construction"],
            "shared_corpus_recall_claimed": False,
            "task": "query-scoped-judged-pool-candidate-retention",
            "unjudged_products_are_irrelevant": False,
            "unjudged_treatment": contract["unjudged_treatment"],
        },
        "per_query": per_query,
        "pipeline": pipeline_config,
        "pipeline_id": pipeline_id,
        "relevance_policy": policy.to_dict(),
        "schema_version": RUN_SCHEMA_VERSION,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    payload["run_id"] = f"retrieval-{hashlib.sha256(canonical).hexdigest()[:12]}"
    logger.info(
        "retrieval_run_completed",
        extra={
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "pipeline_id": pipeline_id,
            "profile_id": profile.profile_id,
            "query_count": len(per_query),
            "run_id": payload["run_id"],
        },
    )
    return payload
