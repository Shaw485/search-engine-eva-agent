from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from apps.api.main import health, smoke
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


def test_api_health_and_smoke() -> None:
    assert health() == {"status": "ok", "stage": "0"}
    assert (
        smoke(query="wireless mouse", top_k=2, backend="local")["deterministic"] is True
    )


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
