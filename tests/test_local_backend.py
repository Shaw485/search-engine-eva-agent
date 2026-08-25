from __future__ import annotations

from pathlib import Path

import pytest

from search_quality.backends.local import LocalSearchBackend
from search_quality.embedding import DeterministicHashEmbedder, cosine_similarity
from search_quality.models import Product, ProductDocument
from search_quality.sample_data import load_products

SAMPLE_PATH = Path(__file__).parents[1] / "data" / "samples" / "products.json"


@pytest.fixture
def backend() -> LocalSearchBackend:
    embedder = DeterministicHashEmbedder()
    instance = LocalSearchBackend()
    instance.replace_documents(
        [
            ProductDocument(product, tuple(embedder.embed(product.searchable_text)))
            for product in load_products(SAMPLE_PATH)
        ]
    )
    return instance


def test_fixture_contains_ten_unique_products() -> None:
    products = load_products(SAMPLE_PATH)
    assert len(products) == 10
    assert len({product.product_id for product in products}) == 10


def test_bm25_ranks_a_wireless_mouse_first(backend: LocalSearchBackend) -> None:
    hits = backend.search_lexical("wireless mouse", top_k=5)
    assert hits[0].product.category == "Computer Mice"
    assert "Wireless" in hits[0].product.title
    assert hits[0].score > hits[-1].score


def test_vector_search_is_deterministic(backend: LocalSearchBackend) -> None:
    vector = DeterministicHashEmbedder().embed("wireless mouse")
    first = [hit.to_dict() for hit in backend.search_vector(vector)]
    second = [hit.to_dict() for hit in backend.search_vector(vector)]
    assert first == second


def test_hash_embedding_is_stable() -> None:
    embedder = DeterministicHashEmbedder(dimensions=32)
    assert embedder.embed("wireless mouse") == embedder.embed("wireless mouse")


def test_cosine_similarity_does_not_require_normalized_vectors() -> None:
    assert cosine_similarity([2.0, 0.0], [3.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([2.0, 0.0], [0.0, 4.0]) == pytest.approx(0.0)


def test_vector_search_uses_true_cosine_similarity() -> None:
    instance = LocalSearchBackend()
    instance.replace_documents(
        [
            ProductDocument(Product("a", "A"), (10.0, 0.0)),
            ProductDocument(Product("b", "B"), (1.0, 1.0)),
        ]
    )
    hits = instance.search_vector([1.0, 1.0], top_k=2)
    assert [hit.product.product_id for hit in hits] == ["b", "a"]


def test_search_requires_an_index() -> None:
    with pytest.raises(RuntimeError, match="replace_documents"):
        LocalSearchBackend().search_lexical("mouse")


def test_duplicate_product_ids_are_rejected() -> None:
    product = load_products(SAMPLE_PATH)[0]
    embedder = DeterministicHashEmbedder()
    document = ProductDocument(product, tuple(embedder.embed(product.searchable_text)))
    with pytest.raises(ValueError, match="unique"):
        LocalSearchBackend().replace_documents([document, document])


def test_empty_index_replacement_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        LocalSearchBackend().replace_documents([])


def test_replacement_does_not_accumulate_documents(backend: LocalSearchBackend) -> None:
    products = load_products(SAMPLE_PATH)
    embedder = DeterministicHashEmbedder()
    documents = [
        ProductDocument(product, tuple(embedder.embed(product.searchable_text)))
        for product in products
    ]
    query_vector = embedder.embed("wireless mouse")
    before = backend.search_vector(query_vector, top_k=20)
    backend.replace_documents(documents)
    after = backend.search_vector(query_vector, top_k=20)
    assert [hit.to_dict() for hit in before] == [hit.to_dict() for hit in after]
    assert len({hit.product.product_id for hit in after}) == 10


def test_vector_dimension_mismatch_is_rejected(backend: LocalSearchBackend) -> None:
    with pytest.raises(ValueError, match="expected 64"):
        backend.search_vector([1.0, 0.0])


@pytest.mark.parametrize("top_k", [0, -1])
def test_invalid_top_k_is_rejected(backend: LocalSearchBackend, top_k: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        backend.search_lexical("mouse", top_k=top_k)


def test_non_finite_embedding_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        ProductDocument(Product("a", "A"), (float("nan"), 0.0))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"k1": 0.0}, "k1"),
        ({"k1": -1.0}, "k1"),
        ({"k1": float("nan")}, "k1"),
        ({"b": -0.1}, "between 0 and 1"),
        ({"b": 1.1}, "between 0 and 1"),
        ({"b": float("inf")}, "between 0 and 1"),
    ],
)
def test_invalid_bm25_parameters_are_rejected(
    kwargs: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        LocalSearchBackend(**kwargs)


def test_zero_vectors_are_rejected(backend: LocalSearchBackend) -> None:
    with pytest.raises(ValueError, match="zero vector"):
        ProductDocument(Product("a", "A"), (0.0, 0.0))
    with pytest.raises(ValueError, match="zero vector"):
        backend.search_vector([0.0] * 64)


def test_lexical_search_omits_non_matching_documents(
    backend: LocalSearchBackend,
) -> None:
    assert backend.search_lexical("term-not-in-the-fixture", top_k=10) == []
