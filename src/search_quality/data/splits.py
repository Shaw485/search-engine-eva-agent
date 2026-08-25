"""Deterministic, leakage-safe query split planning."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass


class SplitContractError(ValueError):
    """Raised when query identities cannot form a trustworthy split."""


@dataclass(frozen=True, slots=True)
class QueryIdentity:
    """The fields that define one official ESCI query."""

    query_id: int
    query_text: str
    locale: str
    origin_split: str


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """Formal split assignments plus a smoke profile inside dev."""

    assignments: dict[int, str]
    smoke_query_ids: frozenset[int]
    normalized_queries: dict[int, str]


def normalize_query(value: str) -> str:
    """Normalize text only for leakage detection, never for display."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _stable_score(*, seed: str, locale: str, query_key: str) -> str:
    payload = f"{seed}\0{locale}\0{query_key}".encode()
    return hashlib.sha256(payload).hexdigest()


def plan_query_splits(
    queries: Iterable[QueryIdentity],
    *,
    seed: str,
    dev_query_count: int,
    smoke_query_count: int,
    official_train_value: str = "train",
    official_test_value: str = "test",
) -> SplitPlan:
    """Preserve official test and derive dev from official train by query.

    Normalized query text is the assignment unit. This is stricter than grouping
    only by ``query_id`` and prevents differently numbered copies of the same text
    from crossing formal splits.
    """

    if dev_query_count < 1:
        raise ValueError("dev_query_count must be at least 1")
    if smoke_query_count < 1 or smoke_query_count > dev_query_count:
        raise ValueError("smoke_query_count must be between 1 and dev_query_count")

    by_id: dict[int, QueryIdentity] = {}
    for query in queries:
        query_text = query.query_text.strip()
        if not query_text:
            raise SplitContractError(f"query {query.query_id} has empty text")
        if not query.locale.strip():
            raise SplitContractError(f"query {query.query_id} has empty locale")
        if query.origin_split not in {official_train_value, official_test_value}:
            raise SplitContractError(
                f"query {query.query_id} has unsupported official split "
                f"{query.origin_split!r}"
            )
        canonical = QueryIdentity(
            query_id=query.query_id,
            query_text=query_text,
            locale=query.locale,
            origin_split=query.origin_split,
        )
        existing = by_id.get(query.query_id)
        if existing is not None and existing != canonical:
            raise SplitContractError(
                f"query_id {query.query_id} maps to multiple identities"
            )
        by_id[query.query_id] = canonical

    if not by_id:
        raise SplitContractError("no queries are available for splitting")

    normalized_queries = {
        query_id: normalize_query(query.query_text) for query_id, query in by_id.items()
    }
    groups: dict[tuple[str, str], list[int]] = {}
    group_origins: dict[tuple[str, str], set[str]] = {}
    for query_id, query in by_id.items():
        group_key = (query.locale, normalized_queries[query_id])
        groups.setdefault(group_key, []).append(query_id)
        group_origins.setdefault(group_key, set()).add(query.origin_split)

    leaked_groups = [key for key, origins in group_origins.items() if len(origins) > 1]
    if leaked_groups:
        sample = leaked_groups[0]
        raise SplitContractError(
            "normalized query text crosses the official train/test boundary: "
            f"locale={sample[0]!r}, query={sample[1]!r}"
        )

    train_groups = [
        key
        for key, origins in group_origins.items()
        if origins == {official_train_value}
    ]
    if len(train_groups) < dev_query_count:
        raise SplitContractError(
            f"need at least {dev_query_count} normalized training queries; "
            f"found {len(train_groups)}"
        )
    train_groups.sort(
        key=lambda key: (_stable_score(seed=seed, locale=key[0], query_key=key[1]), key)
    )

    dev_groups = set(train_groups[:dev_query_count])
    smoke_groups = set(train_groups[:smoke_query_count])
    assignments: dict[int, str] = {}
    smoke_query_ids: set[int] = set()
    for group_key, query_ids in groups.items():
        origins = group_origins[group_key]
        if origins == {official_test_value}:
            split = "test"
        elif group_key in dev_groups:
            split = "dev"
        else:
            split = "train"
        for query_id in query_ids:
            assignments[query_id] = split
            if group_key in smoke_groups:
                smoke_query_ids.add(query_id)

    return SplitPlan(
        assignments=assignments,
        smoke_query_ids=frozenset(smoke_query_ids),
        normalized_queries=normalized_queries,
    )
