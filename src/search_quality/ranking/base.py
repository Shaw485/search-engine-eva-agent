"""Shared contracts for deterministic candidate-set rankers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

ProductKey = tuple[str, str]
RankerConfigValue = str | float | int


@dataclass(frozen=True, slots=True)
class CandidateProduct:
    """The label-free product view exposed to a candidate ranker."""

    locale: str
    product_id: str
    title: str

    def __post_init__(self) -> None:
        if not self.locale.strip():
            raise ValueError("locale must not be empty")
        if not self.product_id.strip():
            raise ValueError("product_id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)


@dataclass(frozen=True, slots=True)
class RankedProduct:
    """One candidate returned by a ranker."""

    locale: str
    product_id: str
    score: float
    rank: int

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)


@runtime_checkable
class CandidateRanker(Protocol):
    """Common result contract used by the Search Evaluation Harness."""

    @property
    def config(self) -> Mapping[str, RankerConfigValue]: ...

    def rank(self, query: str) -> list[RankedProduct]: ...


def validate_candidate_products(
    products: Sequence[CandidateProduct],
) -> tuple[CandidateProduct, ...]:
    """Return a stable snapshot after validating the shared candidate contract."""

    candidates = tuple(products)
    if not candidates:
        raise ValueError("products must not be empty")
    product_keys = [product.key for product in candidates]
    if len(product_keys) != len(set(product_keys)):
        raise ValueError("product keys must be unique")
    return candidates
