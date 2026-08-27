"""OpenSearch adapter implementing the shared Stage 0 backend contract."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import time
from collections.abc import Sequence
from dataclasses import asdict
from http.client import HTTPResponse
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from search_quality.backends.base import MAX_TOP_K
from search_quality.models import Product, ProductDocument, SearchHit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INDEX_CONFIG = PROJECT_ROOT / "configs" / "opensearch" / "products-index.json"
SAFE_INDEX_RE = re.compile(r"search-quality-[a-z0-9][a-z0-9._-]*\Z")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
logger = logging.getLogger("search_quality.backend.opensearch")


class OpenSearchError(RuntimeError):
    """Raised when OpenSearch returns a failed or malformed response."""


class OpenSearchBackend:
    """HTTP-only adapter so the core does not depend on an OpenSearch SDK."""

    name = "opensearch"

    def __init__(
        self,
        *,
        base_url: str,
        index_name: str,
        index_config_path: str | Path = DEFAULT_INDEX_CONFIG,
        username: str = "",
        password: str = "",
        allow_index_reset: bool = False,
        expected_cluster_name: str = "search-quality-local",
        expected_version: str = "3.8.0",
        request_timeout_seconds: float = 5.0,
        readiness_timeout_seconds: float = 45.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.index_name = index_name
        self.username = username
        self.password = password
        self.allow_index_reset = allow_index_reset
        self.expected_cluster_name = expected_cluster_name
        self.expected_version = expected_version
        self.request_timeout_seconds = request_timeout_seconds
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self.index_definition = json.loads(
            Path(index_config_path).read_text(encoding="utf-8")
        )
        self.dimensions = int(
            self.index_definition["mappings"]["properties"]["embedding"]["dimension"]
        )
        parsed_url = urlparse(self.base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or parsed_url.hostname not in LOCAL_HOSTS
        ):
            raise ValueError("Stage 0 OpenSearch smoke is restricted to localhost")
        if parsed_url.username or parsed_url.password:
            raise ValueError(
                "OpenSearch credentials must use dedicated environment fields, "
                "not URL userinfo"
            )
        self._validate_index_name(self.index_name)

    @classmethod
    def from_environment(cls) -> OpenSearchBackend:
        return cls(
            base_url=os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200"),
            index_name=os.environ.get(
                "OPENSEARCH_INDEX", "search-quality-products-smoke"
            ),
            index_config_path=os.environ.get(
                "OPENSEARCH_INDEX_CONFIG", str(DEFAULT_INDEX_CONFIG)
            ),
            username=os.environ.get("OPENSEARCH_USERNAME", ""),
            password=os.environ.get("OPENSEARCH_PASSWORD", ""),
            allow_index_reset=os.environ.get("OPENSEARCH_ALLOW_INDEX_RESET", "").lower()
            == "true",
            expected_cluster_name=os.environ.get(
                "OPENSEARCH_EXPECTED_CLUSTER", "search-quality-local"
            ),
            expected_version=os.environ.get("OPENSEARCH_EXPECTED_VERSION", "3.8.0"),
        )

    def replace_documents(self, documents: Sequence[ProductDocument]) -> None:
        # Re-check and capture the public attribute at the destructive boundary.
        # Constructor validation alone is insufficient because callers can mutate it.
        index_name = self.index_name
        self._validate_index_name(index_name)
        if not documents:
            raise ValueError("documents must not be empty")
        product_ids = [document.product.product_id for document in documents]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("product_id values must be unique")
        if any(len(document.embedding) != self.dimensions for document in documents):
            raise ValueError(f"all embeddings must have {self.dimensions} dimensions")
        if any(
            not all(math.isfinite(value) for value in document.embedding)
            for document in documents
        ):
            raise ValueError("embedding values must be finite")
        if any(
            not any(value != 0.0 for value in document.embedding)
            for document in documents
        ):
            raise ValueError("embedding must not be a zero vector")
        if not self.allow_index_reset:
            raise OpenSearchError(
                "index replacement is disabled; set OPENSEARCH_ALLOW_INDEX_RESET=true "
                "only for the local smoke cluster"
            )

        self.wait_until_ready()
        identity = self._request_json("GET", "/", expected_statuses={200})
        distribution = identity.get("version", {}).get("distribution")
        version = identity.get("version", {}).get("number")
        cluster_name = identity.get("cluster_name")
        if (
            distribution != "opensearch"
            or cluster_name != self.expected_cluster_name
            or version != self.expected_version
        ):
            raise OpenSearchError(
                "refusing destructive smoke reset: expected local OpenSearch cluster "
                f"'{self.expected_cluster_name}' at version {self.expected_version}, "
                f"got distribution={distribution!r}, cluster_name={cluster_name!r}, "
                f"version={version!r}"
            )
        self._request_json(
            "DELETE",
            f"/{quote(index_name, safe='')}",
            expected_statuses={200, 404},
        )
        self._request_json(
            "PUT",
            f"/{quote(index_name, safe='')}",
            payload=self.index_definition,
            expected_statuses={200},
        )

        lines: list[str] = []
        for document in documents:
            lines.append(
                json.dumps(
                    {
                        "index": {
                            "_index": index_name,
                            "_id": document.product.product_id,
                        }
                    },
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            source = asdict(document.product)
            source["embedding"] = list(document.embedding)
            lines.append(json.dumps(source, separators=(",", ":"), allow_nan=False))
        bulk_body = ("\n".join(lines) + "\n").encode("utf-8")
        result = self._request_json(
            "POST",
            "/_bulk?refresh=wait_for",
            raw_body=bulk_body,
            content_type="application/x-ndjson",
            expected_statuses={200},
        )
        if result.get("errors"):
            failure_statuses = [
                int(item.get("index", {}).get("status", 500))
                for item in result.get("items", [])
                if int(item.get("index", {}).get("status", 500)) >= 300
            ]
            raise OpenSearchError(
                "OpenSearch bulk indexing failed for "
                f"{len(failure_statuses)} documents; "
                f"statuses={sorted(set(failure_statuses))}"
            )

    def search_lexical(self, query: str, top_k: int = 5) -> list[SearchHit]:
        self._validate_text_search(query, top_k)
        result = self._request_json(
            "POST",
            f"/{quote(self.index_name, safe='')}/_search",
            payload={
                "size": top_k,
                "_source": [
                    "product_id",
                    "title",
                    "description",
                    "brand",
                    "category",
                ],
                "query": {
                    "multi_match": {
                        "query": query,
                        "fields": [
                            "title^3",
                            "brand^2",
                            "category^1.5",
                            "description",
                        ],
                    }
                },
                "sort": [{"_score": "desc"}, {"product_id": "asc"}],
            },
            expected_statuses={200},
        )
        return self._parse_hits(result, strategy="bm25")

    def search_vector(
        self, query_vector: Sequence[float], top_k: int = 5
    ) -> list[SearchHit]:
        self._validate_vector_search(query_vector, top_k)
        result = self._request_json(
            "POST",
            f"/{quote(self.index_name, safe='')}/_search",
            payload={
                "size": top_k,
                "_source": [
                    "product_id",
                    "title",
                    "description",
                    "brand",
                    "category",
                ],
                "query": {
                    "knn": {
                        "embedding": {
                            "vector": list(query_vector),
                            "k": top_k,
                        }
                    }
                },
                "sort": [{"_score": "desc"}, {"product_id": "asc"}],
            },
            expected_statuses={200},
        )
        return self._parse_hits(result, strategy="vector")

    def wait_until_ready(self) -> None:
        started = time.perf_counter()
        deadline = time.monotonic() + self.readiness_timeout_seconds
        last_error: Exception | None = None
        attempt_count = 0
        while time.monotonic() < deadline:
            attempt_count += 1
            try:
                health = self._request_json(
                    "GET",
                    "/_cluster/health?wait_for_status=yellow&timeout=1s",
                    expected_statuses={200},
                    log_failures=False,
                )
                if health.get("status") in {"yellow", "green"}:
                    logger.info(
                        "opensearch_readiness_completed",
                        extra={
                            "attempt_count": attempt_count,
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000, 3
                            ),
                        },
                    )
                    return
            except (OpenSearchError, OSError) as exc:
                last_error = exc
            time.sleep(0.5)
        logger.error(
            "opensearch_readiness_failed",
            extra={
                "attempt_count": attempt_count,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_code": "opensearch_readiness_timeout",
                "last_error_type": (
                    type(last_error).__name__ if last_error is not None else "none"
                ),
            },
        )
        raise OSError(
            "OpenSearch did not become ready within "
            f"{self.readiness_timeout_seconds:.0f}s"
        ) from last_error

    def _validate_text_search(self, query: str, top_k: int) -> None:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        if top_k > MAX_TOP_K:
            raise ValueError(f"top_k must be at most {MAX_TOP_K}")

    def _validate_vector_search(
        self, query_vector: Sequence[float], top_k: int
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        if top_k > MAX_TOP_K:
            raise ValueError(f"top_k must be at most {MAX_TOP_K}")
        if len(query_vector) != self.dimensions:
            raise ValueError(
                f"query vector has {len(query_vector)} dimensions; "
                f"expected {self.dimensions}"
            )
        if not all(math.isfinite(value) for value in query_vector):
            raise ValueError("query vector values must be finite")
        if not any(value != 0.0 for value in query_vector):
            raise ValueError("query vector must not be a zero vector")

    @staticmethod
    def _validate_index_name(index_name: str) -> None:
        if not SAFE_INDEX_RE.fullmatch(index_name):
            raise ValueError(
                "OpenSearch smoke index must start with 'search-quality-' and "
                "contain only lowercase letters, numbers, dots, underscores, or dashes"
            )

    @staticmethod
    def _parse_hits(payload: dict[str, Any], *, strategy: str) -> list[SearchHit]:
        raw_hits = payload.get("hits", {}).get("hits", [])
        hits: list[SearchHit] = []
        for rank, item in enumerate(raw_hits, start=1):
            source = item.get("_source") or {}
            hits.append(
                SearchHit(
                    product=Product.from_dict(source),
                    score=float(item.get("_score") or 0.0),
                    rank=rank,
                    strategy=strategy,
                )
            )
        return hits

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        expected_statuses: set[int],
        log_failures: bool = True,
    ) -> dict[str, Any]:
        if payload is not None and raw_body is not None:
            raise ValueError("provide payload or raw_body, not both")
        body = raw_body
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        if self.username:
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        operation = self._request_operation(path)
        started = time.perf_counter()
        failure_log = logger.error if log_failures else logger.debug
        logger.debug(
            "opensearch_request_started",
            extra={"backend_operation": operation, "method": method},
        )
        try:
            response: HTTPResponse
            with urlopen(request, timeout=self.request_timeout_seconds) as response:
                status = response.status
                response_body = response.read()
        except HTTPError as exc:
            if exc.code in expected_statuses:
                response_body = exc.read()
                logger.debug(
                    "opensearch_request_completed",
                    extra={
                        "backend_operation": operation,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                        "method": method,
                        "status_code": exc.code,
                    },
                )
                if not response_body:
                    return {}
                return json.loads(response_body.decode("utf-8"))
            exc.read()
            failure_log(
                "opensearch_request_failed",
                extra={
                    "backend_operation": operation,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_code": "opensearch_http_error",
                    "error_type": type(exc).__name__,
                    "method": method,
                    "status_code": exc.code,
                },
            )
            raise OpenSearchError(
                f"OpenSearch {method} {path} returned status {exc.code}"
            ) from exc
        except URLError as exc:
            failure_log(
                "opensearch_request_failed",
                extra={
                    "backend_operation": operation,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_code": "opensearch_unreachable",
                    "error_type": type(exc).__name__,
                    "method": method,
                },
            )
            raise OSError(
                "cannot reach the configured local OpenSearch service"
            ) from exc
        except OSError as exc:
            failure_log(
                "opensearch_request_failed",
                extra={
                    "backend_operation": operation,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_code": "opensearch_io_failure",
                    "error_type": type(exc).__name__,
                    "method": method,
                },
            )
            raise

        if status not in expected_statuses:
            failure_log(
                "opensearch_request_failed",
                extra={
                    "backend_operation": operation,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_code": "opensearch_unexpected_status",
                    "method": method,
                    "status_code": status,
                },
            )
            raise OpenSearchError(
                f"OpenSearch {method} {path} returned unexpected status {status}"
            )
        logger.debug(
            "opensearch_request_completed",
            extra={
                "backend_operation": operation,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "method": method,
                "status_code": status,
            },
        )
        if not response_body:
            return {}
        try:
            return json.loads(response_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            failure_log(
                "opensearch_response_rejected",
                extra={
                    "backend_operation": operation,
                    "error_code": "opensearch_invalid_json",
                    "error_type": type(exc).__name__,
                    "method": method,
                    "status_code": status,
                },
            )
            raise OpenSearchError(
                f"OpenSearch {method} {path} returned invalid JSON"
            ) from exc

    @staticmethod
    def _request_operation(path: str) -> str:
        if path == "/":
            return "cluster_identity"
        if path.startswith("/_cluster/health"):
            return "cluster_health"
        if path.startswith("/_bulk"):
            return "bulk_index"
        if path.endswith("/_search"):
            return "search"
        return "index_admin"
