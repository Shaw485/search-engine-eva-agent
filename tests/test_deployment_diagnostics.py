from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_all_documented_uvicorn_entry_points_disable_access_logs() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    service = (ROOT / "deploy/search-engine-eva-agent.service").read_text(
        encoding="utf-8"
    )
    assert "apps.api.main:app --host 127.0.0.1 --port 8000 --no-access-log" in makefile
    assert "--port 8010 --no-access-log" in service


def test_proxy_disables_request_line_access_log() -> None:
    nginx = (ROOT / "deploy/nginx-search-eval.conf").read_text(encoding="utf-8")
    assert "access_log off;" in nginx


def test_catalog_artifact_is_read_only_and_configured_for_service_user() -> None:
    service = (ROOT / "deploy/search-engine-eva-agent.service").read_text(
        encoding="utf-8"
    )
    deployment = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "User=www-data" in service
    assert (
        "SEARCH_CATALOG_INDEX=/var/lib/search-engine-eva-agent/"
        "catalog-baseline-v1.sqlite3"
    ) in service
    assert "SEARCH_LOG_LEVEL_CATALOG=WARNING" in service
    assert "-o root -g www-data -m 0640" in deployment
    assert "/catalog/search" in deployment
