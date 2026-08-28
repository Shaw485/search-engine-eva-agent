"""Label-blind retrieval channels and deterministic pipeline composition."""

from .contracts import (
    ChannelResult,
    FusedHit,
    RetrievalDocument,
    RetrievalHit,
    RrfContribution,
    SearchPipelineResult,
    StageHit,
)
from .pipeline import QueryScopedSearchPipeline
from .rrf import reciprocal_rank_fuse
from .title_channels import (
    ExactTitleRetriever,
    MultiFieldBM25Retriever,
    TitleBM25Retriever,
)

__all__ = [
    "ChannelResult",
    "ExactTitleRetriever",
    "FusedHit",
    "MultiFieldBM25Retriever",
    "QueryScopedSearchPipeline",
    "RetrievalDocument",
    "RetrievalHit",
    "RrfContribution",
    "SearchPipelineResult",
    "StageHit",
    "TitleBM25Retriever",
    "reciprocal_rank_fuse",
]
