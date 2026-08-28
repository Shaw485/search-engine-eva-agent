"""Reciprocal-rank fusion with strict provenance validation."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from .contracts import ChannelResult, FusedHit, RrfContribution

logger = logging.getLogger("search_quality.retrieval.rrf")


def reciprocal_rank_fuse(
    channels: Sequence[ChannelResult],
    *,
    rrf_k: int = 60,
    top_k: int = 20,
    weights: Mapping[str, float] | None = None,
) -> tuple[FusedHit, ...]:
    """Fuse ranked channel outputs without using incomparable raw scores."""

    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int):
        raise TypeError("rrf_k must be an integer")
    if rrf_k < 1:
        raise ValueError("rrf_k must be at least 1")
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise TypeError("top_k must be an integer")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    snapshot = tuple(channels)
    if not snapshot:
        raise ValueError("at least one channel is required")
    channel_ids = [channel.channel_id for channel in snapshot]
    if len(channel_ids) != len(set(channel_ids)):
        raise ValueError("channel IDs must be unique")
    unknown_weights = set(weights or {}) - set(channel_ids)
    if unknown_weights:
        raise ValueError(f"weights contain unknown channels: {sorted(unknown_weights)}")
    normalized_weights: dict[str, float] = {}
    for channel_id in channel_ids:
        weight = (weights or {}).get(channel_id, 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise TypeError("RRF weights must be real numbers")
        numeric = float(weight)
        if not math.isfinite(numeric) or numeric <= 0.0:
            raise ValueError("RRF weights must be finite and positive")
        normalized_weights[channel_id] = numeric

    contributions: dict[tuple[str, str], list[RrfContribution]] = defaultdict(list)
    for channel in snapshot:
        for hit in channel.hits:
            contribution = normalized_weights[channel.channel_id] / (rrf_k + hit.rank)
            contributions[hit.key].append(
                RrfContribution(
                    channel_id=channel.channel_id,
                    source_rank=hit.rank,
                    contribution=contribution,
                )
            )
    scored = [
        (
            key,
            math.fsum(item.contribution for item in items),
            tuple(sorted(items, key=lambda item: item.channel_id)),
        )
        for key, items in contributions.items()
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    fused = tuple(
        FusedHit(
            locale=key[0],
            product_id=key[1],
            rank=rank,
            score=score,
            contributions=items,
        )
        for rank, (key, score, items) in enumerate(scored[:top_k], start=1)
    )
    logger.debug(
        "rrf_fusion_completed",
        extra={
            "channel_count": len(snapshot),
            "result_count": len(fused),
            "union_count": len(contributions),
        },
    )
    return fused
