from __future__ import annotations

import pytest

from search_quality.data.splits import (
    QueryIdentity,
    SplitContractError,
    normalize_query,
    plan_query_splits,
)


def query(query_id: int, text: str, origin_split: str = "train") -> QueryIdentity:
    return QueryIdentity(query_id, text, "us", origin_split)


def test_query_normalization_is_unicode_case_and_whitespace_aware() -> None:
    assert normalize_query("  ＩPhone   PRO\tCase ") == "iphone pro case"


def test_split_plan_is_deterministic_and_preserves_official_test() -> None:
    queries = [
        query(1, "wireless mouse"),
        query(2, "wired mouse"),
        query(3, "phone case"),
        query(4, "running shoes"),
        query(5, "mechanical keyboard"),
        query(99, "frozen query", "test"),
    ]
    first = plan_query_splits(
        queries, seed="fixture", dev_query_count=2, smoke_query_count=1
    )
    second = plan_query_splits(
        reversed(queries), seed="fixture", dev_query_count=2, smoke_query_count=1
    )

    assert first == second
    assert first.assignments[99] == "test"
    assert set(first.smoke_query_ids) <= {
        query_id for query_id, split in first.assignments.items() if split == "dev"
    }
    assert len(first.smoke_query_ids) == 1


def test_normalized_copies_are_kept_in_the_same_split() -> None:
    queries = [
        query(1, "Wireless  Mouse"),
        query(2, "wireless mouse"),
        query(3, "phone case"),
        query(4, "running shoes"),
    ]
    plan = plan_query_splits(
        queries, seed="fixture", dev_query_count=1, smoke_query_count=1
    )
    assert plan.assignments[1] == plan.assignments[2]


def test_normalized_query_cannot_cross_official_train_and_test() -> None:
    with pytest.raises(SplitContractError, match="crosses"):
        plan_query_splits(
            [query(1, "Wireless Mouse"), query(2, "wireless mouse", "test")],
            seed="fixture",
            dev_query_count=1,
            smoke_query_count=1,
        )


def test_query_id_cannot_map_to_multiple_identities() -> None:
    with pytest.raises(SplitContractError, match="multiple identities"):
        plan_query_splits(
            [query(1, "mouse"), query(1, "keyboard")],
            seed="fixture",
            dev_query_count=1,
            smoke_query_count=1,
        )
