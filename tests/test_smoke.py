from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from apps.api.main import SmokeRequest, app, health, smoke, smoke_post
from search_quality.smoke import run_smoke

SAMPLE_PATH = Path(__file__).parents[1] / "data" / "samples" / "products.json"


def test_local_smoke_contract() -> None:
    result = run_smoke(
        backend_name="local",
        query="wireless mouse",
        top_k=3,
        sample_path=SAMPLE_PATH,
    )
    assert result["backend"] == "local"
    assert result["product_count"] == 10
    assert result["deterministic"] is True
    assert result["repeatable_after_reindex"] is True
    assert len(result["bm25"]) == 3
    assert len(result["vector"]) == 3


def test_api_health_and_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable():
        raise FileNotFoundError("catalog fixture intentionally absent")

    class InactiveServing:
        def readiness(self):
            return {
                "index_id": "catalog-baseline-v1-fixture",
                "mode": "baseline",
                "ready": True,
                "strategy_id": "catalog-baseline-v1",
                "strategy_revision": None,
            }

    monkeypatch.setattr("apps.api.main.get_catalog_search_service", unavailable)
    monkeypatch.setattr(
        "apps.api.main.get_active_catalog_search_service",
        InactiveServing,
    )
    assert health() == {
        "active_serving": {
            "index_id": "catalog-baseline-v1-fixture",
            "mode": "baseline",
            "ready": False,
            "status": "inactive",
            "strategy_id": "catalog-baseline-v1",
            "strategy_revision": None,
        },
        "catalog": {"status": "unavailable"},
        "stage": "catalog-baseline-plus-active-retrieval",
        "status": "ok",
    }
    assert (
        smoke(query="wireless mouse", top_k=2, backend="local")["deterministic"] is True
    )
    assert smoke_post(SmokeRequest(query="wireless mouse", top_k=2))["deterministic"]


def test_api_local_preview_cors_is_restricted() -> None:
    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls.__name__ == "CORSMiddleware"
    )
    assert cors.kwargs["allow_origins"] == [
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ]
    assert cors.kwargs["allow_methods"] == ["GET", "POST"]


def test_api_rejects_unknown_backend() -> None:
    with pytest.raises(HTTPException) as captured:
        smoke(query="wireless mouse", top_k=2, backend="unknown")
    assert captured.value.status_code == 400


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": "   "}, "query must not be empty"),
        ({"query": "!!!"}, "zero vector"),
        ({"top_k": 0}, "between 1 and 10000"),
        ({"top_k": 10_001}, "between 1 and 10000"),
    ],
)
def test_smoke_rejects_invalid_inputs_before_search(
    kwargs: dict[str, str | int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        run_smoke(sample_path=SAMPLE_PATH, **kwargs)
