"""Strict label-blind contracts for retrieval and stage handoffs."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from search_quality.ranking.base import ProductKey

SAFE_COMPONENT_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


def _require_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value.strip()


def _require_component_id(value: str, *, field: str) -> str:
    normalized = _require_text(value, field=field)
    if not SAFE_COMPONENT_ID.fullmatch(normalized):
        raise ValueError(f"{field} must be a safe component ID")
    return normalized


def _require_finite(value: float, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field} must be finite")
    return normalized


def _canonical_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")
    payload = dict(config)
    json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return payload


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    """The only product view a retrieval channel may inspect."""

    locale: str
    product_id: str
    title: str
    brand: str = ""
    bullet_point: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "locale", _require_text(self.locale, field="locale"))
        object.__setattr__(
            self, "product_id", _require_text(self.product_id, field="product_id")
        )
        object.__setattr__(self, "title", _require_text(self.title, field="title"))
        for field in ("brand", "bullet_point", "description"):
            value = getattr(self, field)
            if not isinstance(value, str):
                raise TypeError(f"{field} must be a string")
            object.__setattr__(self, field, value.strip())

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    channel_id: str
    locale: str
    product_id: str
    rank: int
    score: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "channel_id",
            _require_component_id(self.channel_id, field="channel_id"),
        )
        object.__setattr__(self, "locale", _require_text(self.locale, field="locale"))
        object.__setattr__(
            self, "product_id", _require_text(self.product_id, field="product_id")
        )
        if isinstance(self.rank, bool) or not isinstance(self.rank, int):
            raise TypeError("rank must be an integer")
        if self.rank < 1:
            raise ValueError("rank must be at least 1")
        object.__setattr__(self, "score", _require_finite(self.score, field="score"))

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "channel_id": self.channel_id,
            "locale": self.locale,
            "product_id": self.product_id,
            "rank": self.rank,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class ChannelResult:
    channel_id: str
    config: Mapping[str, Any]
    hits: tuple[RetrievalHit, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "channel_id",
            _require_component_id(self.channel_id, field="channel_id"),
        )
        object.__setattr__(self, "config", _canonical_config(self.config))
        object.__setattr__(self, "hits", tuple(self.hits))
        if any(hit.channel_id != self.channel_id for hit in self.hits):
            raise ValueError("all hits must belong to the declared channel")
        _validate_ranked_keys(self.hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "config": dict(self.config),
            "hits": [hit.to_dict() for hit in self.hits],
            "returned_count": len(self.hits),
        }


@dataclass(frozen=True, slots=True)
class RrfContribution:
    channel_id: str
    source_rank: int
    contribution: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "channel_id",
            _require_component_id(self.channel_id, field="channel_id"),
        )
        if isinstance(self.source_rank, bool) or not isinstance(self.source_rank, int):
            raise TypeError("source_rank must be an integer")
        if self.source_rank < 1:
            raise ValueError("source_rank must be at least 1")
        value = _require_finite(self.contribution, field="contribution")
        if value <= 0.0:
            raise ValueError("contribution must be positive")
        object.__setattr__(self, "contribution", value)

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "channel_id": self.channel_id,
            "contribution": self.contribution,
            "source_rank": self.source_rank,
        }


@dataclass(frozen=True, slots=True)
class FusedHit:
    locale: str
    product_id: str
    rank: int
    score: float
    contributions: tuple[RrfContribution, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "locale", _require_text(self.locale, field="locale"))
        object.__setattr__(
            self, "product_id", _require_text(self.product_id, field="product_id")
        )
        if isinstance(self.rank, bool) or not isinstance(self.rank, int):
            raise TypeError("rank must be an integer")
        if self.rank < 1:
            raise ValueError("rank must be at least 1")
        score = _require_finite(self.score, field="score")
        contributions = tuple(self.contributions)
        if not contributions:
            raise ValueError("fused hit must have at least one contribution")
        channel_ids = [item.channel_id for item in contributions]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("fused contributions must use unique channels")
        if not math.isclose(
            score,
            math.fsum(item.contribution for item in contributions),
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ValueError("fused score must equal its RRF contributions")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "contributions", contributions)

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contributions": [item.to_dict() for item in self.contributions],
            "locale": self.locale,
            "product_id": self.product_id,
            "rank": self.rank,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class StageHit:
    stage_id: str
    locale: str
    product_id: str
    rank: int
    score: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stage_id",
            _require_component_id(self.stage_id, field="stage_id"),
        )
        object.__setattr__(self, "locale", _require_text(self.locale, field="locale"))
        object.__setattr__(
            self, "product_id", _require_text(self.product_id, field="product_id")
        )
        if isinstance(self.rank, bool) or not isinstance(self.rank, int):
            raise TypeError("rank must be an integer")
        if self.rank < 1:
            raise ValueError("rank must be at least 1")
        object.__setattr__(self, "score", _require_finite(self.score, field="score"))

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "locale": self.locale,
            "product_id": self.product_id,
            "rank": self.rank,
            "score": self.score,
            "stage_id": self.stage_id,
        }


@dataclass(frozen=True, slots=True)
class SearchPipelineResult:
    schema_version: str
    pipeline_id: str
    config: Mapping[str, Any]
    channels: tuple[ChannelResult, ...]
    recall_union: tuple[ProductKey, ...]
    fused_hits: tuple[FusedHit, ...]
    coarse_hits: tuple[StageHit, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "query-scoped-search-pipeline-result-v1":
            raise ValueError("unsupported search pipeline result schema")
        object.__setattr__(
            self,
            "pipeline_id",
            _require_component_id(self.pipeline_id, field="pipeline_id"),
        )
        object.__setattr__(self, "config", _canonical_config(self.config))
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "recall_union", tuple(self.recall_union))
        object.__setattr__(self, "fused_hits", tuple(self.fused_hits))
        object.__setattr__(self, "coarse_hits", tuple(self.coarse_hits))
        channel_ids = [channel.channel_id for channel in self.channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("pipeline channel IDs must be unique")
        expected_union = sorted(
            {hit.key for channel in self.channels for hit in channel.hits}
        )
        if list(self.recall_union) != expected_union:
            raise ValueError("recall union must equal the channel output union")
        _validate_ranked_keys(self.fused_hits)
        _validate_ranked_keys(self.coarse_hits)
        union_keys = set(self.recall_union)
        fused_keys = {hit.key for hit in self.fused_hits}
        coarse_keys = {hit.key for hit in self.coarse_hits}
        if not fused_keys <= union_keys:
            raise ValueError("fusion output must be a subset of recall union")
        if not coarse_keys <= fused_keys:
            raise ValueError("coarse output must be a subset of fusion output")

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": dict(self.config),
            "pipeline_id": self.pipeline_id,
            "schema_version": self.schema_version,
            "stages": {
                "coarse_rank": {
                    "hits": [hit.to_dict() for hit in self.coarse_hits],
                    "returned_count": len(self.coarse_hits),
                    "stage_id": "coarse-title-bm25-v1",
                },
                "fine_rank": {"status": "not_implemented"},
                "fusion": {
                    "hits": [hit.to_dict() for hit in self.fused_hits],
                    "returned_count": len(self.fused_hits),
                    "stage_id": "rrf-v1",
                },
                "recall_channels": [channel.to_dict() for channel in self.channels],
                "recall_union": {
                    "product_keys": [list(key) for key in self.recall_union],
                    "returned_count": len(self.recall_union),
                    "stage_id": "recall-union-v1",
                },
                "rerank": {"status": "not_implemented"},
            },
        }


@runtime_checkable
class Retriever(Protocol):
    @property
    def channel_id(self) -> str: ...

    @property
    def config(self) -> Mapping[str, Any]: ...

    def search(self, query: str, *, top_k: int) -> tuple[RetrievalHit, ...]: ...


def validate_retrieval_documents(
    documents: Sequence[RetrievalDocument],
) -> tuple[RetrievalDocument, ...]:
    snapshot = tuple(documents)
    if not snapshot:
        raise ValueError("documents must not be empty")
    keys = [document.key for document in snapshot]
    if len(keys) != len(set(keys)):
        raise ValueError("retrieval document keys must be unique")
    return snapshot


def _validate_ranked_keys(hits: Sequence[Any]) -> None:
    keys = [hit.key for hit in hits]
    if len(keys) != len(set(keys)):
        raise ValueError("ranked hits must use unique product keys")
    if [hit.rank for hit in hits] != list(range(1, len(hits) + 1)):
        raise ValueError("ranked hits must use contiguous one-based ranks")
