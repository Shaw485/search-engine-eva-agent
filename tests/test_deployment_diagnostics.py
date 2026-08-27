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
