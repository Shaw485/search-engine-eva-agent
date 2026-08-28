"""Strict semantic validation for query-scoped retrieval Run evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from search_quality.ranking import CandidateProduct, CandidateTitleBM25Ranker
from search_quality.ranking.base import ProductKey
from search_quality.retrieval import (
    ChannelResult,
    QueryScopedSearchPipeline,
    RetrievalDocument,
    RetrievalHit,
    RrfContribution,
    StageHit,
    reciprocal_rank_fuse,
)

from .metrics import ndcg_at_k, recall_at_k, reciprocal_rank_at_k
from .relevance import RelevancePolicy

RUN_SCHEMA_VERSION = "query-scoped-retrieval-run-v1"
RUN_ID_PATTERN = re.compile(r"retrieval-[0-9a-f]{12}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
METRIC_NAMES = (
    "judged_recall@5",
    "judged_recall@10",
    "mrr@10",
    "ndcg@10",
)
EPSILON = 1e-12


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if value != value.strip():
        raise ValueError(f"{field} must be canonical trimmed text")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _same_number(observed: Any, expected: float, field: str) -> None:
    value = _number(observed, field)
    if not math.isclose(value, expected, rel_tol=0.0, abs_tol=EPSILON):
        raise ValueError(f"{field} does not match recomputed evidence")


def _validate_content_id(run: dict[str, Any], *, role: str) -> None:
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"{role} Run has an invalid content ID")
    payload = {key: value for key, value in run.items() if key != "run_id"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    expected = hashlib.sha256(canonical).hexdigest()[:12]
    if run_id != f"retrieval-{expected}":
        raise ValueError(f"{role} Run content does not match its content ID")


def _validate_metric_dict(
    observed: Any,
    expected: Mapping[str, float],
    *,
    field: str,
) -> None:
    payload = _object(observed, field)
    if set(payload) != set(METRIC_NAMES):
        raise ValueError(f"{field} has unexpected metric keys")
    for name, expected_value in expected.items():
        _same_number(payload[name], expected_value, f"{field}.{name}")


def _ranking_metrics(
    keys: Sequence[ProductKey],
    *,
    judgments: Mapping[ProductKey, dict[str, Any]],
    policy: RelevancePolicy,
) -> dict[str, float]:
    relevant = [policy.is_relevant(judgments[key]["label"]) for key in keys]
    gains = [float(judgments[key]["gain"]) for key in keys]
    candidate_gains = [float(item["gain"]) for item in judgments.values()]
    total_relevant = sum(
        policy.is_relevant(str(item["label"])) for item in judgments.values()
    )
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


def _validate_ranked_item(
    item: dict[str, Any],
    *,
    judgments: Mapping[ProductKey, dict[str, Any]],
    locale: str,
    expected_rank: int,
    field: str,
) -> ProductKey:
    item_locale = _text(item.get("locale"), f"{field}.locale")
    product_id = _text(item.get("product_id"), f"{field}.product_id")
    key = (item_locale, product_id)
    if item_locale != locale or key not in judgments:
        raise ValueError(f"{field} is outside its judged Query pool")
    if _integer(item.get("rank"), f"{field}.rank", minimum=1) != expected_rank:
        raise ValueError(f"{field} ranks must be contiguous")
    _number(item.get("score"), f"{field}.score")
    judgment = judgments[key]
    if item.get("label") != judgment["label"]:
        raise ValueError(f"{field} label does not match its judgment")
    _same_number(item.get("gain"), float(judgment["gain"]), f"{field}.gain")
    return key


def _validate_judgments(
    query: dict[str, Any],
    *,
    locale: str,
    policy: RelevancePolicy,
    field: str,
) -> dict[ProductKey, dict[str, Any]]:
    items = _array(query.get("judgments"), f"{field}.judgments")
    if len(items) != _integer(
        query.get("pool_count"), f"{field}.pool_count", minimum=1
    ):
        raise ValueError(f"{field} judgment count does not match pool_count")
    result: dict[ProductKey, dict[str, Any]] = {}
    expected_keys = {
        "gain",
        "label",
        "locale",
        "product_brand",
        "product_bullet_point",
        "product_description",
        "product_id",
        "product_title",
    }
    for index, raw in enumerate(items):
        item = _object(raw, f"{field}.judgments[{index}]")
        if set(item) != expected_keys:
            raise ValueError(f"{field} judgment does not match its schema")
        item_locale = _text(item["locale"], f"{field}.judgment.locale")
        product_id = _text(item["product_id"], f"{field}.judgment.product_id")
        if item_locale != locale:
            raise ValueError(f"{field} judgment crosses locales")
        key = (item_locale, product_id)
        if key in result:
            raise ValueError(f"{field} contains duplicate judgments")
        _text(item["product_title"], f"{field}.judgment.product_title")
        for product_field in (
            "product_brand",
            "product_bullet_point",
            "product_description",
        ):
            _string(item[product_field], f"{field}.judgment.{product_field}")
        label = _text(item["label"], f"{field}.judgment.label")
        if label != label.upper():
            raise ValueError(f"{field} judgment label is not canonical")
        _same_number(item["gain"], policy.gain(label), f"{field}.judgment.gain")
        result[key] = item
    if list(result) != sorted(result):
        raise ValueError(f"{field} judgments must use deterministic key order")
    relevant_count = sum(
        policy.is_relevant(str(item["label"])) for item in result.values()
    )
    if relevant_count < 1 or relevant_count != query.get("relevant_count"):
        raise ValueError(f"{field} relevant_count does not match judgments")
    return result


def _expected_lineage(
    *,
    judgments: Mapping[ProductKey, dict[str, Any]],
    policy: RelevancePolicy,
    channels: Sequence[ChannelResult],
    fused_keys: Sequence[ProductKey],
    coarse_keys: Sequence[ProductKey],
) -> list[dict[str, Any]]:
    route_ranks = {
        channel.channel_id: {hit.key: hit.rank for hit in channel.hits}
        for channel in channels
    }
    union = {hit.key for channel in channels for hit in channel.hits}
    fused_ranks = {key: rank for rank, key in enumerate(fused_keys, start=1)}
    coarse_ranks = {key: rank for rank, key in enumerate(coarse_keys, start=1)}
    result = []
    for key in sorted(
        key
        for key, judgment in judgments.items()
        if policy.is_relevant(str(judgment["label"]))
    ):
        if key not in union:
            loss = "recall"
        elif key not in fused_ranks:
            loss = "fusion"
        elif key not in coarse_ranks:
            loss = "coarse_rank"
        else:
            loss = "retained"
        judgment = judgments[key]
        result.append(
            {
                "coarse_rank": coarse_ranks.get(key),
                "first_loss_stage": loss,
                "fused_rank": fused_ranks.get(key),
                "gain": judgment["gain"],
                "label": judgment["label"],
                "locale": key[0],
                "product_id": key[1],
                "route_ranks": {
                    channel_id: ranks.get(key)
                    for channel_id, ranks in sorted(route_ranks.items())
                },
            }
        )
    return result


def validate_retrieval_run(
    run: dict[str, Any],
    *,
    role: str = "retrieval",
) -> dict[tuple[str, int], dict[str, Any]]:
    """Recompute stage semantics and reject internally inconsistent Run evidence."""

    expected_top = {
        "aggregate",
        "code_revision",
        "dataset",
        "evaluation_boundary",
        "per_query",
        "pipeline",
        "pipeline_id",
        "relevance_policy",
        "run_id",
        "schema_version",
    }
    if not isinstance(run, dict) or set(run) != expected_top:
        raise ValueError(f"{role} Run does not match the retrieval schema")
    if run["schema_version"] != RUN_SCHEMA_VERSION:
        raise ValueError(f"{role} Run uses an unsupported schema")
    if not isinstance(run["code_revision"], str) or not REVISION_PATTERN.fullmatch(
        run["code_revision"]
    ):
        raise ValueError(f"{role} Run has an invalid code revision")
    _validate_content_id(run, role=role)

    boundary = _object(run["evaluation_boundary"], f"{role}.evaluation_boundary")
    required_boundary = {
        "denominator_complete": True,
        "full_catalog_recall_claimed": False,
        "shared_corpus_recall_claimed": False,
        "task": "query-scoped-judged-pool-candidate-retention",
        "unjudged_products_are_irrelevant": False,
    }
    if any(boundary.get(key) != value for key, value in required_boundary.items()):
        raise ValueError(f"{role} Run has an ineligible evaluation boundary")

    policy_payload = _object(run["relevance_policy"], f"{role}.relevance_policy")
    policy = RelevancePolicy.from_dict(policy_payload)
    if policy.to_dict() != policy_payload:
        raise ValueError(f"{role} Run relevance policy is not canonical")

    pipeline = _object(run["pipeline"], f"{role}.pipeline")
    pipeline_id = _text(run["pipeline_id"], f"{role}.pipeline_id")
    pipeline_canonical = json.dumps(
        pipeline,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if pipeline_id != f"pipeline-{hashlib.sha256(pipeline_canonical).hexdigest()[:12]}":
        raise ValueError(f"{role} pipeline ID does not match its config")
    channel_configs = _array(pipeline.get("channels"), f"{role}.pipeline.channels")
    channel_config_by_id: dict[str, dict[str, Any]] = {}
    for raw in channel_configs:
        config = _object(raw, f"{role}.pipeline.channel")
        channel_id = _text(config.get("channel_id"), f"{role}.pipeline.channel_id")
        if channel_id in channel_config_by_id:
            raise ValueError(f"{role} pipeline has duplicate channel IDs")
        channel_config_by_id[channel_id] = config
    if not channel_config_by_id:
        raise ValueError(f"{role} pipeline needs at least one channel")
    channel_top_k = _integer(
        pipeline.get("channel_top_k"), f"{role}.pipeline.channel_top_k", minimum=1
    )
    fusion_config = _object(pipeline.get("fusion"), f"{role}.pipeline.fusion")
    fusion_top_k = _integer(
        fusion_config.get("top_k"), f"{role}.pipeline.fusion.top_k", minimum=1
    )
    rrf_k = _integer(
        fusion_config.get("rrf_k"), f"{role}.pipeline.fusion.rrf_k", minimum=1
    )
    weights_value = fusion_config.get("weights")
    if weights_value == "uniform":
        weights = None
    else:
        weights_payload = _object(weights_value, f"{role}.pipeline.fusion.weights")
        if set(weights_payload) != set(channel_config_by_id):
            raise ValueError(f"{role} RRF weights do not match the channels")
        weights = {
            channel_id: _number(value, f"{role}.pipeline.weight")
            for channel_id, value in weights_payload.items()
        }
        if any(value <= 0.0 for value in weights.values()):
            raise ValueError(f"{role} RRF weights must be positive")
    coarse_config = _object(pipeline.get("coarse_rank"), f"{role}.pipeline.coarse")
    coarse_top_k = _integer(
        coarse_config.get("top_k"), f"{role}.pipeline.coarse.top_k", minimum=1
    )

    queries: dict[tuple[str, int], dict[str, Any]] = {}
    aggregate_values: dict[str, dict[str, list[float]]] = {}
    loss_counts: Counter[str] = Counter()
    unique_relevant_by_channel: Counter[str] = Counter()
    exact_unique_relevant_count = 0
    zero_result_queries = 0
    total_judgments = 0
    for query_index, raw_query in enumerate(
        _array(run["per_query"], f"{role}.per_query")
    ):
        query = _object(raw_query, f"{role}.per_query[{query_index}]")
        expected_query_keys = {
            "judgments",
            "lineage",
            "locale",
            "metrics",
            "pool_count",
            "query_id",
            "query_text",
            "rankings",
            "relevant_count",
            "stage_counts",
        }
        if set(query) != expected_query_keys:
            raise ValueError(f"{role} Query does not match its schema")
        locale = _text(query["locale"], f"{role}.Query.locale")
        query_id = _integer(query["query_id"], f"{role}.Query.query_id", minimum=1)
        query_text = _text(query["query_text"], f"{role}.Query.query_text")
        query_key = (locale, query_id)
        if query_key in queries:
            raise ValueError(f"{role} Run contains duplicate Query keys")
        judgments = _validate_judgments(
            query,
            locale=locale,
            policy=policy,
            field=f"{role}.Query[{query_id}]",
        )
        total_judgments += len(judgments)
        documents = [
            RetrievalDocument(
                brand=str(item["product_brand"]),
                bullet_point=str(item["product_bullet_point"]),
                description=str(item["product_description"]),
                locale=key[0],
                product_id=key[1],
                title=str(item["product_title"]),
            )
            for key, item in judgments.items()
        ]
        expected_pipeline = QueryScopedSearchPipeline(
            documents,
            variant=_text(pipeline.get("variant"), f"{role}.pipeline.variant"),
        )
        if (
            expected_pipeline.config != pipeline
            or expected_pipeline.pipeline_id != pipeline_id
        ):
            raise ValueError(f"{role} pipeline config is not an implemented variant")
        expected_pipeline_result = expected_pipeline.run(query_text)
        expected_channels = {
            channel.channel_id: channel for channel in expected_pipeline_result.channels
        }

        rankings = _object(query["rankings"], f"{role}.Query.rankings")
        if set(rankings) != {"coarse_rank", "fusion", "recall_channels"}:
            raise ValueError(f"{role} Query rankings do not match their schema")
        recall_payload = _object(
            rankings["recall_channels"], f"{role}.Query.recall_channels"
        )
        if set(recall_payload) != set(channel_config_by_id):
            raise ValueError(f"{role} Query recall channels do not match pipeline")
        if set(expected_channels) != set(channel_config_by_id):
            raise ValueError(f"{role} implemented channels do not match pipeline")
        channels: list[ChannelResult] = []
        channel_keys: dict[str, set[ProductKey]] = {}
        for channel_id, config in channel_config_by_id.items():
            raw_hits = _array(recall_payload[channel_id], f"{role}.{channel_id}")
            if len(raw_hits) > channel_top_k:
                raise ValueError(f"{role} channel exceeds top_k")
            hits = []
            keys: set[ProductKey] = set()
            for rank, raw_hit in enumerate(raw_hits, start=1):
                item = _object(raw_hit, f"{role}.{channel_id}[{rank}]")
                expected_keys = {
                    "channel_id",
                    "gain",
                    "label",
                    "locale",
                    "product_id",
                    "rank",
                    "score",
                }
                if set(item) != expected_keys or item["channel_id"] != channel_id:
                    raise ValueError(f"{role} channel hit does not match its schema")
                key = _validate_ranked_item(
                    item,
                    judgments=judgments,
                    locale=locale,
                    expected_rank=rank,
                    field=f"{role}.{channel_id}[{rank}]",
                )
                if key in keys:
                    raise ValueError(f"{role} channel contains duplicate products")
                keys.add(key)
                hits.append(
                    RetrievalHit(
                        channel_id=channel_id,
                        locale=key[0],
                        product_id=key[1],
                        rank=rank,
                        score=float(item["score"]),
                    )
                )
            channels.append(
                ChannelResult(channel_id=channel_id, config=config, hits=tuple(hits))
            )
            if channels[-1].to_dict() != expected_channels[channel_id].to_dict():
                raise ValueError(
                    f"{role} {channel_id} evidence does not match its retriever"
                )
            channel_keys[channel_id] = keys

        expected_fused = reciprocal_rank_fuse(
            channels,
            rrf_k=rrf_k,
            top_k=fusion_top_k,
            weights=weights,
        )
        raw_fused = _array(rankings["fusion"], f"{role}.Query.fusion")
        if len(raw_fused) != len(expected_fused):
            raise ValueError(f"{role} fusion length does not match channel evidence")
        fused_keys: list[ProductKey] = []
        for rank, (raw_hit, expected_hit) in enumerate(
            zip(raw_fused, expected_fused, strict=True), start=1
        ):
            item = _object(raw_hit, f"{role}.fusion[{rank}]")
            if set(item) != {
                "contributions",
                "gain",
                "label",
                "locale",
                "product_id",
                "rank",
                "score",
            }:
                raise ValueError(f"{role} fusion hit does not match its schema")
            key = _validate_ranked_item(
                item,
                judgments=judgments,
                locale=locale,
                expected_rank=rank,
                field=f"{role}.fusion[{rank}]",
            )
            contributions = tuple(
                RrfContribution(
                    channel_id=_text(raw["channel_id"], "RRF channel_id"),
                    source_rank=_integer(
                        raw["source_rank"], "RRF source_rank", minimum=1
                    ),
                    contribution=_number(raw["contribution"], "RRF contribution"),
                )
                for raw in (
                    _object(value, "RRF contribution")
                    for value in _array(item["contributions"], "RRF contributions")
                )
            )
            observed_core = {
                "contributions": [value.to_dict() for value in contributions],
                "locale": key[0],
                "product_id": key[1],
                "rank": rank,
                "score": float(item["score"]),
            }
            if observed_core != expected_hit.to_dict():
                raise ValueError(f"{role} fusion evidence does not match RRF")
            fused_keys.append(key)

        raw_coarse = _array(rankings["coarse_rank"], f"{role}.Query.coarse")
        expected_products = [
            CandidateProduct(
                locale=key[0],
                product_id=key[1],
                title=str(judgments[key]["product_title"]),
            )
            for key in fused_keys
        ]
        expected_ranked = (
            CandidateTitleBM25Ranker(expected_products).rank(str(query["query_text"]))
            if expected_products
            else ()
        )
        expected_coarse = [
            StageHit(
                stage_id="coarse-title-bm25-v1",
                locale=item.locale,
                product_id=item.product_id,
                rank=rank,
                score=item.score,
            ).to_dict()
            for rank, item in enumerate(expected_ranked[:coarse_top_k], start=1)
        ]
        coarse_keys: list[ProductKey] = []
        observed_coarse = []
        for rank, raw_hit in enumerate(raw_coarse, start=1):
            item = _object(raw_hit, f"{role}.coarse[{rank}]")
            if set(item) != {
                "gain",
                "label",
                "locale",
                "product_id",
                "rank",
                "score",
                "stage_id",
            }:
                raise ValueError(f"{role} coarse hit does not match its schema")
            key = _validate_ranked_item(
                item,
                judgments=judgments,
                locale=locale,
                expected_rank=rank,
                field=f"{role}.coarse[{rank}]",
            )
            coarse_keys.append(key)
            observed_coarse.append(
                {
                    "locale": key[0],
                    "product_id": key[1],
                    "rank": rank,
                    "score": float(item["score"]),
                    "stage_id": item["stage_id"],
                }
            )
        if observed_coarse != expected_coarse:
            raise ValueError(f"{role} coarse evidence does not match its ranker")

        channel_metrics = {
            channel.channel_id: _ranking_metrics(
                [hit.key for hit in channel.hits], judgments=judgments, policy=policy
            )
            for channel in channels
        }
        fusion_metrics = _ranking_metrics(
            fused_keys, judgments=judgments, policy=policy
        )
        coarse_metrics = _ranking_metrics(
            coarse_keys, judgments=judgments, policy=policy
        )
        metrics = _object(query["metrics"], f"{role}.Query.metrics")
        if set(metrics) != {
            "coarse_rank",
            "fusion",
            "recall_channels",
            "recall_union",
        }:
            raise ValueError(f"{role} Query metrics do not match their schema")
        observed_channel_metrics = _object(
            metrics["recall_channels"], f"{role}.Query.channel_metrics"
        )
        if set(observed_channel_metrics) != set(channel_metrics):
            raise ValueError(f"{role} Query channel metrics do not match pipeline")
        for channel_id, expected in channel_metrics.items():
            _validate_metric_dict(
                observed_channel_metrics[channel_id],
                expected,
                field=f"{role}.{channel_id}.metrics",
            )
        _validate_metric_dict(metrics["fusion"], fusion_metrics, field=f"{role}.fusion")
        _validate_metric_dict(
            metrics["coarse_rank"], coarse_metrics, field=f"{role}.coarse"
        )
        union = set().union(*(channel_keys.values()))
        relevant_union = sum(
            policy.is_relevant(str(judgments[key]["label"])) for key in union
        )
        union_coverage = relevant_union / int(query["relevant_count"])
        recall_union = _object(metrics["recall_union"], f"{role}.recall_union")
        if set(recall_union) != {"judged_relevant_coverage"}:
            raise ValueError(f"{role} recall-union metric schema is invalid")
        _same_number(
            recall_union["judged_relevant_coverage"],
            union_coverage,
            f"{role}.recall_union.coverage",
        )

        expected_lineage = _expected_lineage(
            judgments=judgments,
            policy=policy,
            channels=channels,
            fused_keys=fused_keys,
            coarse_keys=coarse_keys,
        )
        if query["lineage"] != expected_lineage:
            raise ValueError(f"{role} lineage does not match stage rankings")
        loss_counts.update(item["first_loss_stage"] for item in expected_lineage)
        stage_counts = _object(query["stage_counts"], f"{role}.stage_counts")
        if stage_counts != {
            "coarse_output": len(coarse_keys),
            "fusion_output": len(fused_keys),
            "recall_union": len(union),
        }:
            raise ValueError(f"{role} stage counts do not match rankings")
        if not fused_keys:
            zero_result_queries += 1

        for stage_id, expected in {
            "coarse-title-bm25-v1": coarse_metrics,
            "rrf-v1": fusion_metrics,
            **channel_metrics,
        }.items():
            for metric_name, value in expected.items():
                aggregate_values.setdefault(stage_id, {}).setdefault(
                    metric_name, []
                ).append(value)
        aggregate_values.setdefault("recall-union-v1", {}).setdefault(
            "judged_relevant_coverage", []
        ).append(union_coverage)
        title_keys = channel_keys.get("title-bm25-recall-v1", set())
        exact_keys = channel_keys.get("exact-title-recall-v1", set())
        exact_unique_relevant_count += sum(
            key in exact_keys
            and key not in title_keys
            and policy.is_relevant(str(judgment["label"]))
            for key, judgment in judgments.items()
        )
        for channel_id, keys in channel_keys.items():
            other_keys = set().union(
                *(value for key, value in channel_keys.items() if key != channel_id)
            )
            unique_relevant_by_channel[channel_id] += sum(
                key in keys
                and key not in other_keys
                and policy.is_relevant(str(judgment["label"]))
                for key, judgment in judgments.items()
            )
        queries[query_key] = query

    if not queries:
        raise ValueError(f"{role} Run contains no Query evidence")
    aggregate = _object(run["aggregate"], f"{role}.aggregate")
    if aggregate.get("first_loss_counts") != {
        stage: loss_counts.get(stage, 0)
        for stage in ("recall", "fusion", "coarse_rank", "retained")
    }:
        raise ValueError(f"{role} aggregate first-loss counts are inconsistent")
    if aggregate.get("exact_unique_relevant_count") != exact_unique_relevant_count:
        raise ValueError(f"{role} exact-channel contribution is inconsistent")
    if aggregate.get("unique_relevant_by_channel") != {
        channel_id: unique_relevant_by_channel.get(channel_id, 0)
        for channel_id in channel_config_by_id
    }:
        raise ValueError(f"{role} per-channel contribution is inconsistent")
    if aggregate.get("zero_result_queries") != zero_result_queries:
        raise ValueError(f"{role} zero-result count is inconsistent")
    aggregate_stages = _object(aggregate.get("stages"), f"{role}.aggregate.stages")
    if set(aggregate_stages) != set(aggregate_values):
        raise ValueError(f"{role} aggregate stages do not match Query evidence")
    for stage_id, metric_values in aggregate_values.items():
        expected = {
            f"mean_{name}": math.fsum(values) / len(values)
            for name, values in metric_values.items()
        }
        observed = _object(aggregate_stages[stage_id], f"{role}.{stage_id}")
        if set(observed) != set(expected):
            raise ValueError(f"{role} aggregate metric keys are inconsistent")
        for name, value in expected.items():
            _same_number(observed[name], value, f"{role}.{stage_id}.{name}")

    dataset = _object(run["dataset"], f"{role}.dataset")
    if dataset.get("profile") != "smoke":
        raise ValueError(f"{role} retrieval Run must be smoke-only")
    if dataset.get("query_count") != len(queries):
        raise ValueError(f"{role} dataset Query count is inconsistent")
    if dataset.get("judged_pairs") != total_judgments:
        raise ValueError(f"{role} dataset judgment count is inconsistent")
    if dataset.get("possible_pairs_in_scope") != total_judgments:
        raise ValueError(f"{role} dataset denominator is incomplete")
    if dataset.get("unknown_pairs_in_scope") != 0:
        raise ValueError(f"{role} dataset has unknown in-scope pairs")
    for key in (
        "canonical_sha256",
        "file_sha256",
        "query_keys_sha256",
        "stage1_manifest_sha256",
    ):
        if not isinstance(dataset.get(key), str) or not SHA256_PATTERN.fullmatch(
            dataset[key]
        ):
            raise ValueError(f"{role} dataset has an invalid {key}")
    return queries
