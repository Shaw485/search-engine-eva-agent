"""End-to-end Stage 0 smoke contract."""

from __future__ import annotations

import argparse
import json
import math
import os
import threading
from pathlib import Path
from typing import Any

from .backends.base import MAX_TOP_K, SearchBackend
from .backends.local import LocalSearchBackend
from .embedding import DeterministicHashEmbedder, EmbeddingProvider
from .models import ProductDocument
from .sample_data import load_products

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SAMPLE_PATH = PROJECT_ROOT / "data" / "samples" / "products.json"
_OPENSEARCH_SMOKE_LOCK = threading.Lock()


def create_backend(name: str) -> SearchBackend:
    if name == "local":
        return LocalSearchBackend()
    if name == "opensearch":
        from .backends.opensearch import OpenSearchBackend

        return OpenSearchBackend.from_environment()
    raise ValueError(f"unsupported backend: {name}")


def run_smoke(
    *,
    backend_name: str = "local",
    query: str = "wireless mouse",
    top_k: int = 5,
    sample_path: str | Path = DEFAULT_SAMPLE_PATH,
) -> dict[str, Any]:
    if backend_name == "opensearch":
        # The Stage 0 adapter rebuilds one fixed local index. Serialize API calls
        # in this process so delete/create/bulk sequences cannot interleave.
        with _OPENSEARCH_SMOKE_LOCK:
            return _run_smoke(
                backend_name=backend_name,
                query=query,
                top_k=top_k,
                sample_path=sample_path,
            )
    return _run_smoke(
        backend_name=backend_name,
        query=query,
        top_k=top_k,
        sample_path=sample_path,
    )


def _run_smoke(
    *,
    backend_name: str,
    query: str,
    top_k: int,
    sample_path: str | Path,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")

    products = load_products(sample_path)
    embedder: EmbeddingProvider = DeterministicHashEmbedder()
    query_vector = embedder.embed(query)
    if not all(math.isfinite(value) for value in query_vector):
        raise ValueError("query embedding values must be finite")
    if not any(value != 0.0 for value in query_vector):
        raise ValueError("query produced a zero vector; use searchable ASCII terms")
    documents = [
        ProductDocument(
            product=product,
            embedding=tuple(embedder.embed(product.searchable_text)),
        )
        for product in products
    ]
    backend = create_backend(backend_name)
    backend.replace_documents(documents)

    first_bm25 = backend.search_lexical(query, top_k)
    first_vector = backend.search_vector(query_vector, top_k)

    # Rebuild the same index to verify replacement is idempotent and cannot
    # accumulate duplicate products across smoke runs.
    backend.replace_documents(documents)
    second_bm25 = backend.search_lexical(query, top_k)
    second_vector = backend.search_vector(query_vector, top_k)

    expected_count = min(top_k, len(products))
    for strategy, hits in (("bm25", second_bm25), ("vector", second_vector)):
        if strategy == "vector" and len(hits) != expected_count:
            raise RuntimeError(
                f"{strategy} returned {len(hits)} hits; expected {expected_count}"
            )
        if len(hits) > expected_count:
            raise RuntimeError(
                f"{strategy} returned {len(hits)} hits; maximum is {expected_count}"
            )
        product_ids = [hit.product.product_id for hit in hits]
        if len(product_ids) != len(set(product_ids)):
            raise RuntimeError(f"{strategy} returned duplicate products")
        if [hit.rank for hit in hits] != list(range(1, len(hits) + 1)):
            raise RuntimeError(f"{strategy} returned invalid ranks")
        if any(not math.isfinite(hit.score) for hit in hits):
            raise RuntimeError(f"{strategy} returned a non-finite score")

    bm25_payload = [hit.to_dict() for hit in first_bm25]
    vector_payload = [hit.to_dict() for hit in first_vector]
    deterministic = bm25_payload == [
        hit.to_dict() for hit in second_bm25
    ] and vector_payload == [hit.to_dict() for hit in second_vector]
    if not deterministic:
        raise RuntimeError("repeated searches returned different rankings")

    return {
        "backend": backend.name,
        "embedding_provider": embedder.name,
        "embedding_dimensions": embedder.dimensions,
        "query": query,
        "product_count": len(products),
        "top_k": top_k,
        "deterministic": deterministic,
        "repeatable_after_reindex": deterministic,
        "bm25": bm25_payload,
        "vector": vector_payload,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("local", "opensearch"),
        default=os.environ.get("SEARCH_BACKEND", "local"),
    )
    parser.add_argument("--query", default="wireless mouse")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--sample-path",
        default=os.environ.get("SEARCH_SAMPLE_PATH", str(DEFAULT_SAMPLE_PATH)),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_smoke(
        backend_name=args.backend,
        query=args.query,
        top_k=args.top_k,
        sample_path=args.sample_path,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
