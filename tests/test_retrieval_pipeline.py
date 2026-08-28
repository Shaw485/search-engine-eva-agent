from __future__ import annotations

import math

import pytest

from search_quality.retrieval import (
    ChannelResult,
    ExactTitleRetriever,
    MultiFieldBM25Retriever,
    QueryScopedSearchPipeline,
    RetrievalDocument,
    RetrievalHit,
    TitleBM25Retriever,
    reciprocal_rank_fuse,
)


def _documents() -> list[RetrievalDocument]:
    return [
        RetrievalDocument("us", "p1", "Wireless Mouse 2.4G"),
        RetrievalDocument("us", "p2", "Wireless Keyboard"),
        RetrievalDocument("us", "p3", "Wired Mouse"),
        RetrievalDocument("us", "p4", "Ceramic Coffee Cup"),
    ]


def test_title_channels_are_deterministic_label_blind_and_do_not_pad_zeros() -> None:
    bm25 = TitleBM25Retriever(_documents())
    exact = ExactTitleRetriever(_documents())

    first = bm25.search("wireless mouse", top_k=50)
    second = bm25.search("wireless mouse", top_k=50)
    strict = exact.search("wireless mouse", top_k=50)

    assert first == second
    assert [hit.product_id for hit in first] == ["p1", "p2", "p3"]
    assert [hit.product_id for hit in strict] == ["p1"]
    assert all(hit.score > 0 for hit in first)
    assert "p4" not in {hit.product_id for hit in first}
    assert not hasattr(_documents()[0], "esci_label")


def test_exact_channel_supports_exact_product_identifier() -> None:
    hits = ExactTitleRetriever(_documents()).search("p4", top_k=5)

    assert [(hit.product_id, hit.rank, hit.score) for hit in hits] == [("p4", 1, 8.0)]


def test_multi_field_channel_recovers_a_product_without_title_overlap() -> None:
    documents = _documents() + [
        RetrievalDocument(
            "us",
            "p5",
            "Precision Office Peripheral",
            brand="Acme",
            bullet_point="Silent wireless mouse for travel",
        )
    ]

    title_keys = {
        hit.key
        for hit in TitleBM25Retriever(documents).search("silent mouse", top_k=50)
    }
    multi_field_keys = {
        hit.key
        for hit in MultiFieldBM25Retriever(documents).search("silent mouse", top_k=50)
    }

    assert ("us", "p5") not in title_keys
    assert ("us", "p5") in multi_field_keys


def test_rrf_matches_hand_calculation_and_uses_stable_ties() -> None:
    first = ChannelResult(
        channel_id="alpha-v1",
        config={"id": "alpha-v1"},
        hits=(
            RetrievalHit("alpha-v1", "us", "p1", 1, 10.0),
            RetrievalHit("alpha-v1", "us", "p2", 2, 9.0),
        ),
    )
    second = ChannelResult(
        channel_id="beta-v1",
        config={"id": "beta-v1"},
        hits=(
            RetrievalHit("beta-v1", "us", "p2", 1, 999.0),
            RetrievalHit("beta-v1", "us", "p3", 2, 1.0),
        ),
    )

    fused = reciprocal_rank_fuse((first, second), rrf_k=60, top_k=3)

    assert [hit.product_id for hit in fused] == ["p2", "p1", "p3"]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].score == pytest.approx(1 / 61)
    assert fused[2].score == pytest.approx(1 / 62)
    assert math.fsum(
        item.contribution for item in fused[0].contributions
    ) == pytest.approx(fused[0].score)


def test_retrieval_contracts_fail_closed_on_corrupt_outputs() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        ChannelResult(
            channel_id="alpha-v1",
            config={},
            hits=(RetrievalHit("alpha-v1", "us", "p1", 2, 1.0),),
        )
    with pytest.raises(ValueError, match="unique"):
        ChannelResult(
            channel_id="alpha-v1",
            config={},
            hits=(
                RetrievalHit("alpha-v1", "us", "p1", 1, 1.0),
                RetrievalHit("alpha-v1", "us", "p1", 2, 0.5),
            ),
        )
    valid = ChannelResult(
        channel_id="alpha-v1",
        config={},
        hits=(RetrievalHit("alpha-v1", "us", "p1", 1, 1.0),),
    )
    with pytest.raises(ValueError, match="unknown channels"):
        reciprocal_rank_fuse((valid,), weights={"unknown-v1": 1.0})
    with pytest.raises(ValueError, match="finite"):
        RetrievalHit("alpha-v1", "us", "p1", 1, float("nan"))


def test_pipeline_tracks_channel_union_fusion_and_coarse_subsets() -> None:
    pipeline = QueryScopedSearchPipeline(
        _documents(),
        channel_top_k=50,
        fusion_top_k=3,
        coarse_top_k=2,
    )

    first = pipeline.run("wireless mouse")
    second = pipeline.run("wireless mouse")

    assert first == second
    assert first.pipeline_id.startswith("pipeline-")
    assert set(first.recall_union) == {
        ("us", "p1"),
        ("us", "p2"),
        ("us", "p3"),
    }
    assert {hit.key for hit in first.fused_hits} <= set(first.recall_union)
    assert {hit.key for hit in first.coarse_hits} <= {
        hit.key for hit in first.fused_hits
    }
    assert first.to_dict()["stages"]["fine_rank"] == {"status": "not_implemented"}


def test_weighted_multi_field_variant_records_and_applies_fixed_rrf_weights() -> None:
    pipeline = QueryScopedSearchPipeline(
        _documents(),
        variant="title-exact-multifield-weighted-v1",
    )

    result = pipeline.run("wireless mouse")

    assert pipeline.config["fusion"]["weights"] == {
        "exact-title-recall-v1": 1.0,
        "multi-field-bm25-recall-v1": 0.1,
        "title-bm25-recall-v1": 1.0,
    }
    assert [channel.channel_id for channel in result.channels] == [
        "title-bm25-recall-v1",
        "exact-title-recall-v1",
        "multi-field-bm25-recall-v1",
    ]
    assert result.fused_hits[0].contributions


def test_channels_reject_queries_without_searchable_tokens() -> None:
    for retriever in (
        TitleBM25Retriever(_documents()),
        ExactTitleRetriever(_documents()),
    ):
        with pytest.raises(ValueError, match="searchable token"):
            retriever.search("___", top_k=10)
