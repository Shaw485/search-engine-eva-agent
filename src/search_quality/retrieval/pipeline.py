"""A versioned query-scoped retrieval, fusion and coarse-ranking pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from search_quality.ranking import CandidateProduct, CandidateTitleBM25Ranker

from .contracts import (
    ChannelResult,
    RetrievalDocument,
    SearchPipelineResult,
    StageHit,
    validate_retrieval_documents,
)
from .rrf import reciprocal_rank_fuse
from .title_channels import (
    ExactTitleRetriever,
    MultiFieldBM25Retriever,
    TitleBM25Retriever,
)

PIPELINE_RESULT_SCHEMA_VERSION = "query-scoped-search-pipeline-result-v1"


class QueryScopedSearchPipeline:
    """Run two label-blind channels, RRF, then a cheap lexical coarse ranker."""

    def __init__(
        self,
        documents: Sequence[RetrievalDocument],
        *,
        channel_top_k: int = 50,
        fusion_top_k: int = 20,
        coarse_top_k: int = 10,
        rrf_k: int = 60,
        variant: str = "title-exact-v1",
    ) -> None:
        snapshot = validate_retrieval_documents(documents)
        for name, value in (
            ("channel_top_k", channel_top_k),
            ("fusion_top_k", fusion_top_k),
            ("coarse_top_k", coarse_top_k),
            ("rrf_k", rrf_k),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        if coarse_top_k > fusion_top_k:
            raise ValueError("coarse_top_k must not exceed fusion_top_k")
        if variant not in {
            "title-exact-v1",
            "title-exact-multifield-v1",
            "title-exact-multifield-weighted-v1",
            "title-exact-multifield-weighted-aggressive-v1",
        }:
            raise ValueError("unsupported query-scoped pipeline variant")
        self.channel_top_k = channel_top_k
        self.fusion_top_k = fusion_top_k
        self.coarse_top_k = coarse_top_k
        self.rrf_k = rrf_k
        self.variant = variant
        self._documents = {document.key: document for document in snapshot}
        channels = [
            TitleBM25Retriever(snapshot),
            ExactTitleRetriever(snapshot),
        ]
        if variant == "title-exact-multifield-v1":
            channels.append(MultiFieldBM25Retriever(snapshot))
        if variant in {
            "title-exact-multifield-weighted-v1",
            "title-exact-multifield-weighted-aggressive-v1",
        }:
            channels.append(MultiFieldBM25Retriever(snapshot))
        self._channels = tuple(channels)
        if variant == "title-exact-multifield-weighted-v1":
            self._fusion_weights = {
                "exact-title-recall-v1": 1.0,
                "multi-field-bm25-recall-v1": 0.1,
                "title-bm25-recall-v1": 1.0,
            }
        elif variant == "title-exact-multifield-weighted-aggressive-v1":
            self._fusion_weights = {
                "exact-title-recall-v1": 0.5,
                "multi-field-bm25-recall-v1": 0.25,
                "title-bm25-recall-v1": 1.0,
            }
        else:
            self._fusion_weights = None
        config = {
            "analyzer_id": "ascii-alnum-lower-v1",
            "channel_top_k": channel_top_k,
            "channels": [dict(channel.config) for channel in self._channels],
            "coarse_rank": {
                "ranker_id": "candidate-title-bm25-v1",
                "top_k": coarse_top_k,
            },
            "fine_rank": {"status": "not_implemented"},
            "fusion": {
                "method": "reciprocal_rank_fusion",
                "rrf_k": rrf_k,
                "top_k": fusion_top_k,
                "weights": self._fusion_weights or "uniform",
            },
            "rerank": {"status": "not_implemented"},
            "schema_version": "query-scoped-search-pipeline-config-v1",
            "variant": variant,
        }
        canonical = json.dumps(
            config,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        self.config = config
        self.pipeline_id = f"pipeline-{hashlib.sha256(canonical).hexdigest()[:12]}"

    def run(self, query: str) -> SearchPipelineResult:
        channel_results = tuple(
            ChannelResult(
                channel_id=channel.channel_id,
                config=channel.config,
                hits=channel.search(query, top_k=self.channel_top_k),
            )
            for channel in self._channels
        )
        recall_union = tuple(
            sorted(
                {
                    hit.key
                    for channel_result in channel_results
                    for hit in channel_result.hits
                }
            )
        )
        fused = reciprocal_rank_fuse(
            channel_results,
            rrf_k=self.rrf_k,
            top_k=self.fusion_top_k,
            weights=self._fusion_weights,
        )
        coarse = self._coarse_rank(query, fused)
        return SearchPipelineResult(
            schema_version=PIPELINE_RESULT_SCHEMA_VERSION,
            pipeline_id=self.pipeline_id,
            config=self.config,
            channels=channel_results,
            recall_union=recall_union,
            fused_hits=fused,
            coarse_hits=coarse,
        )

    def _coarse_rank(self, query: str, fused) -> tuple[StageHit, ...]:
        if not fused:
            return ()
        products = [
            CandidateProduct(
                locale=self._documents[hit.key].locale,
                product_id=self._documents[hit.key].product_id,
                title=self._documents[hit.key].title,
            )
            for hit in fused
        ]
        ranked = CandidateTitleBM25Ranker(products).rank(query)
        return tuple(
            StageHit(
                stage_id="coarse-title-bm25-v1",
                locale=item.locale,
                product_id=item.product_id,
                rank=rank,
                score=item.score,
            )
            for rank, item in enumerate(ranked[: self.coarse_top_k], start=1)
        )
