from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from search_quality.backends.opensearch import OpenSearchBackend, OpenSearchError
from search_quality.models import Product, ProductDocument

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "configs" / "opensearch" / "products-index.json"


def document(product_id: str = "p001") -> ProductDocument:
    return ProductDocument(
        Product(product_id, "Wireless Mouse", brand="Example"),
        (1.0,) + (0.0,) * 63,
    )


class RecordingOpenSearchBackend(OpenSearchBackend):
    def __init__(self, *, identity: dict[str, Any] | None = None) -> None:
        super().__init__(
            base_url="http://127.0.0.1:9200",
            index_name="search-quality-contract-test",
            index_config_path=CONFIG,
            allow_index_reset=True,
        )
        self.calls: list[dict[str, Any]] = []
        self.identity = identity or {
            "cluster_name": "search-quality-local",
            "version": {"distribution": "opensearch", "number": "3.8.0"},
        }

    def wait_until_ready(self) -> None:
        return None

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        expected_statuses: set[int],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "raw_body": raw_body,
                "content_type": content_type,
                "expected_statuses": expected_statuses,
            }
        )
        if path == "/":
            return self.identity
        if path.startswith("/_bulk"):
            return {"errors": False, "items": []}
        if path.endswith("/_search"):
            return {"hits": {"hits": []}}
        return {}


def test_opensearch_index_config_is_valid_and_pinned_to_64_dimensions() -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert payload["settings"]["index"]["knn"] is True
    assert payload["settings"]["index"]["number_of_replicas"] == 0
    embedding = payload["mappings"]["properties"]["embedding"]
    assert embedding["dimension"] == 64
    assert embedding["method"]["engine"] == "lucene"
    assert embedding["method"]["space_type"] == "cosinesimil"


def test_opensearch_adapter_rejects_wrong_vector_size_without_network() -> None:
    backend = OpenSearchBackend(
        base_url="http://127.0.0.1:9200",
        index_name="search-quality-unused",
        index_config_path=CONFIG,
    )
    with pytest.raises(ValueError, match="expected 64"):
        backend.search_vector([1.0, 0.0], top_k=2)


@pytest.mark.parametrize(
    "index_name",
    ["*", "_all", "customer-production", "search-quality-foo,bar"],
)
def test_opensearch_adapter_rejects_unsafe_index_names(index_name: str) -> None:
    with pytest.raises(ValueError, match="must start"):
        OpenSearchBackend(
            base_url="http://127.0.0.1:9200",
            index_name=index_name,
            index_config_path=CONFIG,
        )


def test_opensearch_adapter_rejects_remote_hosts() -> None:
    with pytest.raises(ValueError, match="localhost"):
        OpenSearchBackend(
            base_url="https://search.example.com",
            index_name="search-quality-smoke",
            index_config_path=CONFIG,
        )


def test_index_reset_requires_explicit_opt_in() -> None:
    backend = OpenSearchBackend(
        base_url="http://127.0.0.1:9200",
        index_name="search-quality-smoke",
        index_config_path=CONFIG,
    )
    with pytest.raises(OpenSearchError, match="disabled"):
        backend.replace_documents([document()])


def test_index_reset_checks_cluster_identity_before_delete() -> None:
    backend = RecordingOpenSearchBackend(
        identity={
            "cluster_name": "shared-production",
            "version": {"distribution": "opensearch", "number": "3.8.0"},
        }
    )
    with pytest.raises(OpenSearchError, match="refusing destructive"):
        backend.replace_documents([document()])
    assert [(call["method"], call["path"]) for call in backend.calls] == [("GET", "/")]


def test_bulk_and_search_request_contracts() -> None:
    backend = RecordingOpenSearchBackend()
    backend.replace_documents([document()])
    backend.search_lexical("wireless mouse", top_k=3)
    backend.search_vector([1.0] + [0.0] * 63, top_k=3)

    bulk_call = next(
        call for call in backend.calls if call["path"].startswith("/_bulk")
    )
    assert bulk_call["path"] == "/_bulk?refresh=wait_for"
    assert bulk_call["content_type"] == "application/x-ndjson"
    assert bulk_call["raw_body"].endswith(b"\n")
    lines = bulk_call["raw_body"].decode("utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["index"] == {
        "_index": "search-quality-contract-test",
        "_id": "p001",
    }
    assert json.loads(lines[1])["product_id"] == "p001"

    search_calls = [call for call in backend.calls if call["path"].endswith("/_search")]
    lexical_payload = search_calls[0]["payload"]
    vector_payload = search_calls[1]["payload"]
    assert lexical_payload["sort"] == [{"_score": "desc"}, {"product_id": "asc"}]
    assert vector_payload["query"]["knn"]["embedding"] == {
        "vector": [1.0] + [0.0] * 63,
        "k": 3,
    }
    assert vector_payload["sort"] == [{"_score": "desc"}, {"product_id": "asc"}]


@pytest.mark.parametrize("invalid", [[0.0] * 64, [float("nan")] + [0.0] * 63])
def test_opensearch_adapter_rejects_invalid_cosine_vectors(
    invalid: list[float],
) -> None:
    backend = RecordingOpenSearchBackend()
    with pytest.raises(ValueError, match="finite|zero vector"):
        backend.search_vector(invalid, top_k=2)


def test_opensearch_adapter_rejects_k_above_engine_limit() -> None:
    backend = RecordingOpenSearchBackend()
    with pytest.raises(ValueError, match="at most 10000"):
        backend.search_vector([1.0] + [0.0] * 63, top_k=10_001)
