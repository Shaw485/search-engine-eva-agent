from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _nginx_location(config: str, location: str) -> str:
    pattern = rf"location\s+{re.escape(location)}\s*\{{(?P<body>.*?)\n\}}"
    match = re.search(pattern, config, flags=re.DOTALL)
    assert match is not None, f"missing nginx location: {location}"
    return match.group("body")


def test_all_documented_uvicorn_entry_points_disable_access_logs() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    service = (ROOT / "deploy/search-engine-eva-agent.service").read_text(
        encoding="utf-8"
    )
    assert "apps.api.main:app --host 127.0.0.1 --port 8000 --no-access-log" in makefile
    assert "--port 8010 --no-access-log" in service


def test_proxy_disables_request_line_access_log() -> None:
    nginx = (ROOT / "deploy/nginx-search-eval.conf").read_text(encoding="utf-8")
    decision = _nginx_location(nginx, "= /search-eval-api/agent/strategy/decision")
    public_api = _nginx_location(nginx, "/search-eval-api/")
    assert "access_log off;" in public_api
    assert "auth_basic off;" in public_api
    assert "auth_basic_user_file" not in public_api
    assert "auth_basic off;" in decision
    assert "return 404;" in decision
    assert "proxy_pass" not in decision


def test_agent_page_and_analysis_endpoints_require_basic_auth() -> None:
    nginx = (ROOT / "deploy/nginx-search-eval.conf").read_text(encoding="utf-8")
    agent_page = _nginx_location(nginx, "= /search-agent.html")
    proposal = _nginx_location(nginx, "= /search-eval-api/agent/strategy/propose")
    retrieval = _nginx_location(nginx, "= /search-eval-api/agent/retrieval/analyze")
    agent_eval = _nginx_location(nginx, "= /search-eval-api/agent/eval/run")
    query_constructor = _nginx_location(
        nginx, "= /search-eval-api/agent/query-constructor/build"
    )
    bad_cases = _nginx_location(nginx, "= /search-eval-api/agent/bad-cases/run")

    assert 'auth_basic "Search Agent";' in agent_page
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in agent_page
    assert 'add_header Cache-Control "no-store" always;' in agent_page
    assert "try_files $uri =404;" in agent_page

    assert 'auth_basic "Search Agent";' in proposal
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in proposal
    assert "proxy_pass http://127.0.0.1:8010/agent/strategy/propose;" in proposal
    assert 'proxy_set_header Authorization "";' in proposal

    assert 'auth_basic "Search Agent";' in retrieval
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in retrieval
    assert "proxy_pass http://127.0.0.1:8010/agent/retrieval/analyze;" in retrieval
    assert 'proxy_set_header Authorization "";' in retrieval
    assert "proxy_read_timeout 130s;" in retrieval

    assert 'auth_basic "Search Agent";' in agent_eval
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in agent_eval
    assert "proxy_pass http://127.0.0.1:8010/agent/eval/run;" in agent_eval
    assert 'proxy_set_header Authorization "";' in agent_eval
    assert "proxy_read_timeout 130s;" in agent_eval

    assert 'auth_basic "Search Agent";' in query_constructor
    assert (
        "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in query_constructor
    )
    assert (
        "proxy_pass http://127.0.0.1:8010/agent/query-constructor/build;"
        in query_constructor
    )
    assert 'proxy_set_header Authorization "";' in query_constructor
    assert "proxy_read_timeout 15s;" in query_constructor

    assert 'auth_basic "Search Agent";' in bad_cases
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in bad_cases
    assert "proxy_pass http://127.0.0.1:8010/agent/bad-cases/run;" in bad_cases
    assert 'proxy_set_header Authorization "";' in bad_cases
    assert "proxy_read_timeout 130s;" in bad_cases
    assert 'add_header Cache-Control "no-store" always;' in bad_cases


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


def test_agent_runtime_store_is_the_only_writable_service_path() -> None:
    service = (ROOT / "deploy/search-engine-eva-agent.service").read_text(
        encoding="utf-8"
    )
    deployment = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    runtime = "/var/lib/search-engine-eva-agent/runtime"

    assert "EnvironmentFile=/etc/search-engine-eva-agent.env" in service
    assert f"Environment=SEARCH_AGENT_ARTIFACT_ROOT={runtime}" in service
    assert "Environment=SEARCH_LOG_LEVEL_AGENT_OPTIMIZATION=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_AGENT_EVAL=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_BAD_CASE=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_RETRIEVAL=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_RETRIEVAL_ANALYSIS=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_STAGE_DIAGNOSIS=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_QUERY_CONSTRUCTOR=INFO" in service
    assert "ProtectSystem=strict" in service
    assert "ReadOnlyPaths=/var/www/search-engine-eva-agent" in service
    assert f"ReadWritePaths={runtime}" in service
    assert "UMask=0027" in service
    assert f"-o www-data -g www-data -m 0750 {runtime}" in deployment
    assert "SEARCH_CODE_REVISION=" in deployment
    assert "sudo systemctl daemon-reload" in deployment
