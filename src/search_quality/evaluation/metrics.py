"""Pure, model-independent ranking metrics.

The DCG functions accept final numeric gains, not ESCI labels. The experiment
configuration is responsible for mapping labels such as E/S/C/I to gains. The
reciprocal-rank and success functions accept explicit relevance flags so that
the experiment also has to record which labels count as relevant.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from numbers import Integral, Real


def _validate_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, Integral):
        raise TypeError("k must be an integer")
    if k < 1:
        raise ValueError("k must be at least 1")
    return int(k)


def _validate_gains(gains: Sequence[float]) -> tuple[float, ...]:
    validated: list[float] = []
    for gain in gains:
        if isinstance(gain, bool) or not isinstance(gain, Real):
            raise TypeError("gains must contain only real numbers")
        value = float(gain)
        if not math.isfinite(value):
            raise ValueError("gains must be finite")
        if value < 0.0:
            raise ValueError("gains must be non-negative")
        validated.append(value)
    return tuple(validated)


def _validate_relevance(relevant: Sequence[bool]) -> tuple[bool, ...]:
    validated: list[bool] = []
    for value in relevant:
        if not isinstance(value, bool):
            raise TypeError("relevance values must be booleans")
        validated.append(value)
    return tuple(validated)


def _mean(values: Sequence[float], *, metric_name: str) -> float:
    if not values:
        raise ValueError(f"{metric_name} requires at least one query")
    return math.fsum(values) / len(values)


def dcg_at_k(gains: Sequence[float], k: int) -> float:
    """Return discounted cumulative gain for one ranked result list.

    Rank is one-based, so the discount for a result at rank ``r`` is
    ``log2(r + 1)``. Inputs are direct gains: this function deliberately does
    not apply an implicit ``2**grade - 1`` transformation.
    """

    limit = _validate_k(k)
    validated = _validate_gains(gains)
    return math.fsum(
        gain / math.log2(rank + 1)
        for rank, gain in enumerate(validated[:limit], start=1)
    )


def ndcg_at_k(
    ranked_gains: Sequence[float], *, candidate_gains: Sequence[float], k: int
) -> float:
    """Return DCG normalized by the ideal ordering of all judged candidates.

    ``ranked_gains`` may contain only the returned top results, but
    ``candidate_gains`` must contain the complete judged candidate set for the
    Query. Product-identity validation belongs to the evaluation harness that
    converts ranked product IDs into these gain sequences.
    """

    limit = _validate_k(k)
    validated_ranked = _validate_gains(ranked_gains)
    validated_candidates = _validate_gains(candidate_gains)
    actual = dcg_at_k(validated_ranked, limit)
    ideal = dcg_at_k(sorted(validated_candidates, reverse=True), limit)
    if ideal == 0.0:
        if actual > 0.0:
            raise ValueError("ranked gains are inconsistent with candidate gains")
        return 0.0
    if actual > ideal and not math.isclose(actual, ideal):
        raise ValueError("ranked gains exceed the ideal candidate DCG")
    return actual / ideal


def mean_ndcg_at_k(
    rankings: Iterable[Sequence[float]],
    *,
    candidate_gains_by_query: Iterable[Sequence[float]],
    k: int,
) -> float:
    """Return mean nDCG across Query result lists."""

    limit = _validate_k(k)
    ranked_values = list(rankings)
    candidate_values = list(candidate_gains_by_query)
    if len(ranked_values) != len(candidate_values):
        raise ValueError("rankings and candidate gains must contain the same queries")
    values = [
        ndcg_at_k(ranked, candidate_gains=candidates, k=limit)
        for ranked, candidates in zip(ranked_values, candidate_values, strict=True)
    ]
    return _mean(values, metric_name="mean nDCG")


def reciprocal_rank_at_k(relevant: Sequence[bool], k: int) -> float:
    """Return reciprocal rank of the first relevant result within top-k."""

    limit = _validate_k(k)
    validated = _validate_relevance(relevant)
    for rank, is_relevant in enumerate(validated[:limit], start=1):
        if is_relevant:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank_at_k(rankings: Iterable[Sequence[bool]], k: int) -> float:
    """Return mean reciprocal rank across Query result lists."""

    limit = _validate_k(k)
    values = [reciprocal_rank_at_k(relevant, limit) for relevant in rankings]
    return _mean(values, metric_name="MRR")


def success_at_k(relevant: Sequence[bool], k: int) -> float:
    """Return 1 when top-k contains any relevant result, otherwise 0."""

    limit = _validate_k(k)
    validated = _validate_relevance(relevant)
    return float(any(validated[:limit]))


def mean_success_at_k(rankings: Iterable[Sequence[bool]], k: int) -> float:
    """Return mean Success@K across Query result lists."""

    limit = _validate_k(k)
    values = [success_at_k(relevant, limit) for relevant in rankings]
    return _mean(values, metric_name="mean Success@K")
