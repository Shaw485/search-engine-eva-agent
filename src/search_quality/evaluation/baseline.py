"""Run the deterministic title-BM25 candidate-reranking baseline."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import polars as pl

from search_quality.evaluation.datasets import EvaluationProfile, sha256_file
from search_quality.evaluation.metrics import (
    mean_ndcg_at_k,
    mean_reciprocal_rank_at_k,
    mean_success_at_k,
    ndcg_at_k,
    reciprocal_rank_at_k,
    success_at_k,
)
from search_quality.evaluation.relevance import ESCI_LABEL_SET, RelevancePolicy
from search_quality.ranking import (
    CandidateProduct,
    CandidateTitleBM25Ranker,
    ProductKey,
)

REQUIRED_COLUMNS = {
    "query_id",
    "example_id",
    "query_text",
    "product_id",
    "product_locale",
    "product_title",
    "esci_label",
    "eval_split",
    "origin_split",
    "is_smoke",
}
RUN_SCHEMA_VERSION = "search-evaluation-run-v1"


def _validate_frame(frame: pl.DataFrame) -> None:
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"evaluation data is missing columns: {missing}")
    if frame.is_empty():
        raise ValueError("evaluation data must not be empty")

    for column in (
        "query_text",
        "product_id",
        "product_locale",
        "product_title",
        "esci_label",
        "eval_split",
        "origin_split",
    ):
        invalid = frame.filter(
            pl.col(column).is_null() | (pl.col(column).cast(pl.String) == "")
        )
        if invalid.height:
            raise ValueError(f"evaluation data has empty {column} values")
    if frame.get_column("query_id").null_count():
        raise ValueError("evaluation data has null query_id values")
    if frame.get_column("is_smoke").null_count():
        raise ValueError("evaluation data has null is_smoke values")
    if not frame.schema["query_id"].is_integer():
        raise TypeError("query_id must use an integer dtype")
    if frame.schema["is_smoke"] != pl.Boolean:
        raise TypeError("is_smoke must use a Boolean dtype")

    eval_splits = set(frame.get_column("eval_split").unique().to_list())
    if eval_splits != {"dev"}:
        raise ValueError(
            "routine baseline runs accept only smoke/dev data; "
            f"found splits={sorted(eval_splits)}"
        )
    origin_splits = set(frame.get_column("origin_split").unique().to_list())
    if origin_splits != {"train"}:
        raise ValueError(
            "routine baseline runs accept only official-train-derived data; "
            f"found origin_splits={sorted(origin_splits)}"
        )
    locales = set(frame.get_column("product_locale").unique().to_list())
    if locales != {"us"}:
        raise ValueError(f"expected only the us locale, found {sorted(locales)}")

    invalid_labels = sorted(
        frame.filter(~pl.col("esci_label").is_in(ESCI_LABEL_SET))
        .get_column("esci_label")
        .unique()
        .to_list()
    )
    if invalid_labels:
        raise ValueError(f"evaluation data has invalid labels: {invalid_labels}")

    duplicate_pairs = (
        frame.group_by("product_locale", "query_id", "product_id")
        .len()
        .filter(pl.col("len") != 1)
    )
    if duplicate_pairs.height:
        raise ValueError("evaluation data has duplicate Query-product pairs")

    query_conflicts = (
        frame.group_by("product_locale", "query_id")
        .agg(
            pl.col("query_text").n_unique().alias("texts"),
            pl.col("eval_split").n_unique().alias("splits"),
            pl.col("origin_split").n_unique().alias("origin_splits"),
        )
        .filter(
            (pl.col("texts") != 1)
            | (pl.col("splits") != 1)
            | (pl.col("origin_splits") != 1)
        )
    )
    if query_conflicts.height:
        raise ValueError("a query_id maps to multiple Query texts")

    product_conflicts = (
        frame.group_by("product_locale", "product_id")
        .agg(pl.col("product_title").n_unique().alias("titles"))
        .filter(pl.col("titles") != 1)
    )
    if product_conflicts.height:
        raise ValueError("a product_id maps to multiple product titles")


def _validate_ranking(
    candidate_keys: list[ProductKey], ranked_keys: list[ProductKey]
) -> None:
    if len(ranked_keys) != len(set(ranked_keys)):
        raise ValueError("ranker returned duplicate product keys")
    if set(ranked_keys) != set(candidate_keys):
        missing = sorted(set(candidate_keys) - set(ranked_keys))
        unknown = sorted(set(ranked_keys) - set(candidate_keys))
        raise ValueError(
            f"ranker output does not match candidates; missing={missing[:3]}, "
            f"unknown={unknown[:3]}"
        )


def run_candidate_title_bm25_baseline(
    profile: EvaluationProfile,
    *,
    policy: RelevancePolicy,
    code_revision: str,
) -> dict[str, Any]:
    """Evaluate title BM25 on every judged candidate set in a Parquet profile."""

    path = profile.path
    if not path.is_file():
        raise FileNotFoundError(path)
    code_revision = code_revision.strip()
    if not code_revision:
        raise ValueError("code_revision must not be empty")
    frame = pl.read_parquet(path).with_columns(
        pl.col("query_text").cast(pl.String).str.strip_chars(),
        pl.col("product_id").cast(pl.String).str.strip_chars(),
        pl.col("product_title").cast(pl.String).str.strip_chars(),
        pl.col("esci_label").cast(pl.String).str.to_uppercase(),
        pl.col("product_locale").cast(pl.String).str.to_lowercase(),
        pl.col("eval_split").cast(pl.String).str.to_lowercase(),
        pl.col("origin_split").cast(pl.String).str.to_lowercase(),
    )
    _validate_frame(frame)
    data_sha256 = sha256_file(path)
    if data_sha256 != profile.file_sha256:
        raise ValueError(
            f"{profile.profile_id} file does not match the Stage 1 manifest SHA-256"
        )
    is_smoke = bool(frame.get_column("is_smoke").all())
    if profile.profile_id == "smoke" and not is_smoke:
        raise ValueError("smoke profile must contain only fixed smoke queries")

    products = frame.select(pl.struct("product_locale", "product_id").n_unique()).item()
    queries = frame.select(pl.struct("product_locale", "query_id").n_unique()).item()
    observed_counts = (frame.height, queries, products)
    expected_counts = (
        profile.expected_rows,
        profile.expected_queries,
        profile.expected_products,
    )
    if observed_counts != expected_counts:
        raise ValueError(
            "evaluation profile counts do not match Stage 1 manifest; "
            f"observed={observed_counts}, expected={expected_counts}"
        )

    ranked_gains_by_query: list[list[float]] = []
    candidate_gains_by_query: list[list[float]] = []
    relevance_by_query: list[list[bool]] = []
    per_query: list[dict[str, Any]] = []

    ranker_config: dict[str, str | float | int] | None = None
    for query_frame in frame.sort(
        "product_locale", "query_id", "product_id"
    ).partition_by("product_locale", "query_id", maintain_order=True):
        locale = str(query_frame.item(0, "product_locale"))
        query_id = int(query_frame.item(0, "query_id"))
        query_text = str(query_frame.item(0, "query_text"))
        candidate_keys = [
            (row["product_locale"], row["product_id"])
            for row in query_frame.select("product_locale", "product_id").iter_rows(
                named=True
            )
        ]
        candidate_products = [
            CandidateProduct(
                locale=row["product_locale"],
                product_id=row["product_id"],
                title=row["product_title"],
            )
            for row in query_frame.select(
                "product_locale", "product_id", "product_title"
            ).iter_rows(named=True)
        ]
        ranker = CandidateTitleBM25Ranker(candidate_products)
        if ranker_config is None:
            ranker_config = ranker.config
        elif ranker_config != ranker.config:
            raise ValueError("ranker configuration changed inside one run")
        labels_by_product = dict(
            (
                (row["product_locale"], row["product_id"]),
                row["esci_label"],
            )
            for row in query_frame.select(
                "product_locale", "product_id", "esci_label"
            ).iter_rows(named=True)
        )
        example_ids_by_product = dict(
            (
                (row["product_locale"], row["product_id"]),
                int(row["example_id"]),
            )
            for row in query_frame.select(
                "product_locale", "product_id", "example_id"
            ).iter_rows(named=True)
        )
        ranked = ranker.rank(query_text)
        ranked_keys = [result.key for result in ranked]
        _validate_ranking(candidate_keys, ranked_keys)

        ranked_labels = [labels_by_product[key] for key in ranked_keys]
        ranked_gains = [policy.gain(label) for label in ranked_labels]
        candidate_gains = [
            policy.gain(label)
            for label in query_frame.get_column("esci_label").to_list()
        ]
        relevance = [policy.is_relevant(label) for label in ranked_labels]

        ranked_gains_by_query.append(ranked_gains)
        candidate_gains_by_query.append(candidate_gains)
        relevance_by_query.append(relevance)
        per_query.append(
            {
                "candidate_count": len(candidate_keys),
                "locale": locale,
                "metrics": {
                    "mrr@10": reciprocal_rank_at_k(relevance, 10),
                    "ndcg@10": ndcg_at_k(
                        ranked_gains, candidate_gains=candidate_gains, k=10
                    ),
                    "ndcg@5": ndcg_at_k(
                        ranked_gains, candidate_gains=candidate_gains, k=5
                    ),
                    "success@1": success_at_k(relevance, 1),
                    "success@5": success_at_k(relevance, 5),
                },
                "query_id": query_id,
                "query_text": query_text,
                "ranking": [
                    {
                        "gain": policy.gain(labels_by_product[result.key]),
                        "example_id": example_ids_by_product[result.key],
                        "label": labels_by_product[result.key],
                        "locale": result.locale,
                        "product_id": result.product_id,
                        "rank": result.rank,
                        "score": result.score,
                    }
                    for result in ranked
                ],
            }
        )

    metrics = {
        "mrr@10": mean_reciprocal_rank_at_k(relevance_by_query, 10),
        "ndcg@10": mean_ndcg_at_k(
            ranked_gains_by_query,
            candidate_gains_by_query=candidate_gains_by_query,
            k=10,
        ),
        "ndcg@5": mean_ndcg_at_k(
            ranked_gains_by_query,
            candidate_gains_by_query=candidate_gains_by_query,
            k=5,
        ),
        "success@1": mean_success_at_k(relevance_by_query, 1),
        "success@5": mean_success_at_k(relevance_by_query, 5),
    }
    if not all(
        0.0 <= value <= 1.0 and math.isfinite(value) for value in metrics.values()
    ):
        raise ValueError("baseline produced invalid aggregate metrics")

    payload: dict[str, Any] = {
        "code_revision": code_revision,
        "dataset": {
            **profile.to_manifest_dict(),
            "eval_splits": sorted(frame.get_column("eval_split").unique().to_list()),
            "judgments": frame.height,
            "locales": sorted(frame.get_column("product_locale").unique().to_list()),
            "origin_splits": sorted(
                frame.get_column("origin_split").unique().to_list()
            ),
            "products": products,
            "queries": len(per_query),
        },
        "evaluation_boundary": {
            "full_catalog_recall_claimed": False,
            "task": "judged-candidate-reranking",
            "unjudged_products_are_irrelevant": False,
        },
        "metrics": metrics,
        "per_query": per_query,
        "ranker": ranker_config,
        "relevance_policy": policy.to_dict(),
        "schema_version": RUN_SCHEMA_VERSION,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    payload["run_id"] = f"bm25-{hashlib.sha256(canonical).hexdigest()[:12]}"
    return payload
