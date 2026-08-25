"""Domain models shared by every search backend."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Product:
    """Small, backend-independent product representation."""

    product_id: str
    title: str
    description: str = ""
    brand: str = ""
    category: str = ""

    @property
    def searchable_text(self) -> str:
        return " ".join(
            value.strip()
            for value in (self.title, self.brand, self.category, self.description)
            if value and value.strip()
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Product:
        product_id = str(payload.get("product_id", "")).strip()
        title = str(payload.get("title", "")).strip()
        if not product_id:
            raise ValueError("product_id must not be empty")
        if not title:
            raise ValueError(f"title must not be empty for product {product_id}")
        return cls(
            product_id=product_id,
            title=title,
            description=str(payload.get("description", "")).strip(),
            brand=str(payload.get("brand", "")).strip(),
            category=str(payload.get("category", "")).strip(),
        )


@dataclass(frozen=True, slots=True)
class ProductDocument:
    """A product plus the explicitly versioned vector sent to a backend."""

    product: Product
    embedding: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.embedding:
            raise ValueError("embedding must not be empty")
        if not all(math.isfinite(value) for value in self.embedding):
            raise ValueError("embedding values must be finite")
        if not any(value != 0.0 for value in self.embedding):
            raise ValueError("embedding must not be a zero vector")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Normalized hit returned by local and external backends."""

    product: Product
    score: float
    rank: int
    strategy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": asdict(self.product),
            "score": round(float(self.score), 8),
            "rank": self.rank,
            "strategy": self.strategy,
        }
