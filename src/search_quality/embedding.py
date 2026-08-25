"""Offline deterministic vectors for exercising the Stage 0 k-NN contract.

This is deliberately not presented as a semantic model. Stage 3 replaces it
with a versioned sentence-transformer adapter. Its purpose now is to prove that
indexing, vector dimensions, cosine scoring, and replay are deterministic.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol

from .text import tokenize


class EmbeddingProvider(Protocol):
    """Embedding port kept separate from storage and search backends."""

    name: str
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


class DeterministicHashEmbedder:
    """Map tokens to a stable signed hashing vector without external services."""

    name = "deterministic-hash-v1"

    def __init__(self, dimensions: int = 64) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = tokenize(text)
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have the same dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    return dot_product / (left_norm * right_norm)
