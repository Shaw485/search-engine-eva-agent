from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def _nginx_location(config: str, location: str) -> str:
    marker = f"location {location} "
    start = config.find(marker)
    assert start >= 0, f"missing nginx location: {location}"
    opening = config.find("{", start + len(marker))
    assert opening >= 0, f"missing nginx body: {location}"
    depth = 0
    for index in range(opening, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[opening + 1 : index]
    raise AssertionError(f"unterminated nginx location: {location}")


def _assert_owner_rate_limit(location: str, *, login_probe: bool = False) -> None:
    zone = "search_agent_login" if login_probe else "search_agent_owner_api"
    burst = 3 if login_probe else 15
    assert f"limit_req zone={zone} burst={burst} nodelay;" in location
    assert "limit_req_status 429;" in location
    assert (
        "access_log /var/log/nginx/search-agent-auth-rate-limit.log "
        "search_agent_auth_limit if=$search_agent_auth_rate_limited;"
    ) in location


def _assert_public_analysis_limits(location: str) -> None:
    assert "limit_req zone=search_agent_public_analysis burst=1 nodelay;" in location
    assert "limit_conn search_agent_public_analysis_concurrency 1;" in location
    assert "limit_req_status 429;" in location
    assert "limit_conn_status 429;" in location
    assert "client_max_body_size 1k;" in location
    assert "limit_except POST" in location
    assert "deny all;" in location
    assert (
        "access_log /var/log/nginx/search-agent-public-analysis-rejection.log "
        "search_agent_public_analysis_rejection "
        "if=$search_agent_public_analysis_rejected;"
    ) in location


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


def test_analysis_is_public_but_all_other_agent_compute_remains_owner_only() -> None:
    nginx = (ROOT / "deploy/nginx-search-eval.conf").read_text(encoding="utf-8")
    agent_page = _nginx_location(nginx, "= /search-agent.html")
    auth_check = _nginx_location(nginx, "= /search-agent-auth-check.json")
    auth_failure = _nginx_location(nginx, "= /__search-agent-auth-failed")
    proposal = _nginx_location(nginx, "= /search-eval-api/agent/strategy/propose")
    retrieval = _nginx_location(nginx, "= /search-eval-api/agent/retrieval/analyze")
    agent_eval = _nginx_location(nginx, "= /search-eval-api/agent/eval/run")
    query_constructor = _nginx_location(
        nginx, "= /search-eval-api/agent/query-constructor/build"
    )
    bad_cases = _nginx_location(nginx, "= /search-eval-api/agent/bad-cases/run")
    diagnostic_plan = _nginx_location(
        nginx, "= /search-eval-api/agent/diagnostic-experiments/plan"
    )

    assert "auth_basic off;" in agent_page
    assert "auth_basic_user_file" not in agent_page
    assert 'add_header Cache-Control "no-store" always;' in agent_page
    assert "Content-Security-Policy" in agent_page
    assert "connect-src 'self'" in agent_page
    assert "frame-ancestors 'none'" in agent_page
    assert 'add_header Referrer-Policy "no-referrer" always;' in agent_page
    assert 'add_header X-Content-Type-Options "nosniff" always;' in agent_page
    assert "try_files $uri =404;" in agent_page

    assert 'auth_basic "Search Agent Owner";' in auth_check
    _assert_owner_rate_limit(auth_check, login_probe=True)
    assert "error_log /var/log/nginx/search-agent-auth-critical.log crit;" in (
        auth_check
    )
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in auth_check
    assert "error_page 401 =403 /__search-agent-auth-failed;" in auth_check
    assert 'add_header Cache-Control "no-store" always;' in auth_check
    assert "try_files $uri =404;" in auth_check
    assert "internal;" in auth_failure
    assert "auth_basic off;" in auth_failure
    assert "return 403;" in auth_failure

    assert 'auth_basic "Search Agent Owner";' in proposal
    _assert_owner_rate_limit(proposal)
    assert "error_log /var/log/nginx/search-agent-auth-critical.log crit;" in proposal
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in proposal
    assert "proxy_pass http://127.0.0.1:8010/agent/strategy/propose;" in proposal
    assert 'proxy_set_header Authorization "";' in proposal
    assert "error_page 401 =403 /__search-agent-auth-failed;" in proposal
    assert 'add_header Cache-Control "no-store" always;' in proposal

    assert "auth_basic off;" in retrieval
    assert "auth_basic_user_file" not in retrieval
    assert "error_page 401" not in retrieval
    _assert_public_analysis_limits(retrieval)
    assert "proxy_pass http://127.0.0.1:8010/agent/retrieval/analyze;" in retrieval
    assert 'proxy_set_header Authorization "";' in retrieval
    assert 'proxy_set_header Cookie "";' in retrieval
    assert 'proxy_set_header X-Search-Owner-Principal "";' in retrieval
    assert 'add_header Cache-Control "no-store" always;' in retrieval
    assert "proxy_read_timeout 130s;" in retrieval

    assert 'auth_basic "Search Agent Owner";' in agent_eval
    _assert_owner_rate_limit(agent_eval)
    assert "error_log /var/log/nginx/search-agent-auth-critical.log crit;" in agent_eval
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in agent_eval
    assert "proxy_pass http://127.0.0.1:8010/agent/eval/run;" in agent_eval
    assert 'proxy_set_header Authorization "";' in agent_eval
    assert "error_page 401 =403 /__search-agent-auth-failed;" in agent_eval
    assert 'add_header Cache-Control "no-store" always;' in agent_eval
    assert "proxy_read_timeout 130s;" in agent_eval

    assert 'auth_basic "Search Agent Owner";' in query_constructor
    _assert_owner_rate_limit(query_constructor)
    assert "error_log /var/log/nginx/search-agent-auth-critical.log crit;" in (
        query_constructor
    )
    assert (
        "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in query_constructor
    )
    assert (
        "proxy_pass http://127.0.0.1:8010/agent/query-constructor/build;"
        in query_constructor
    )
    assert 'proxy_set_header Authorization "";' in query_constructor
    assert "error_page 401 =403 /__search-agent-auth-failed;" in query_constructor
    assert 'add_header Cache-Control "no-store" always;' in query_constructor
    assert "proxy_read_timeout 15s;" in query_constructor

    assert 'auth_basic "Search Agent Owner";' in bad_cases
    _assert_owner_rate_limit(bad_cases)
    assert "error_log /var/log/nginx/search-agent-auth-critical.log crit;" in bad_cases
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in bad_cases
    assert "proxy_pass http://127.0.0.1:8010/agent/bad-cases/run;" in bad_cases
    assert 'proxy_set_header Authorization "";' in bad_cases
    assert "error_page 401 =403 /__search-agent-auth-failed;" in bad_cases
    assert "proxy_read_timeout 140s;" in bad_cases
    assert 'add_header Cache-Control "no-store" always;' in bad_cases

    assert 'auth_basic "Search Agent Owner";' in diagnostic_plan
    _assert_owner_rate_limit(diagnostic_plan)
    assert "error_log /var/log/nginx/search-agent-auth-critical.log crit;" in (
        diagnostic_plan
    )
    assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in (
        diagnostic_plan
    )
    assert (
        "proxy_pass http://127.0.0.1:8010/agent/diagnostic-experiments/plan;"
        in diagnostic_plan
    )
    assert 'proxy_set_header Authorization "";' in diagnostic_plan
    assert "error_page 401 =403 /__search-agent-auth-failed;" in diagnostic_plan
    assert 'add_header Cache-Control "no-store" always;' in diagnostic_plan


def test_human_oracle_routes_are_exact_owner_only_and_strip_credentials() -> None:
    nginx = (ROOT / "deploy/nginx-search-eval.conf").read_text(encoding="utf-8")
    route_suffixes = (
        "batches/create",
        "batches/status",
        "intents/view",
        "intents/submit",
        "behaviors/view",
        "behaviors/submit",
        "batches/seal",
    )

    for suffix in route_suffixes:
        external = f"= /search-eval-api/agent/human-oracle/{suffix}"
        internal = f"http://127.0.0.1:8010/agent/human-oracle/{suffix};"
        location = _nginx_location(nginx, external)
        _assert_owner_rate_limit(location)
        assert 'auth_basic "Search Agent Owner";' in location
        assert "error_log /var/log/nginx/search-agent-auth-critical.log crit;" in (
            location
        )
        assert "auth_basic_user_file /etc/nginx/.search-agent.htpasswd;" in location
        assert "error_page 401 =403 /__search-agent-auth-failed;" in location
        assert 'add_header Cache-Control "no-store" always;' in location
        assert f"proxy_pass {internal}" in location
        assert "proxy_set_header X-Search-Owner-Principal $remote_user;" in location
        assert 'proxy_set_header Authorization "";' in location
        expected_timeout = "40s" if suffix == "behaviors/view" else "15s"
        assert f"proxy_read_timeout {expected_timeout};" in location


def test_owner_auth_rate_limits_and_safe_rejection_log_are_http_scoped() -> None:
    http_config = (ROOT / "deploy/nginx-search-agent-rate-limit-http.conf").read_text(
        encoding="utf-8"
    )
    deployment = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")

    assert (
        "limit_req_zone $binary_remote_addr zone=search_agent_login:10m rate=5r/m;"
        in http_config
    )
    assert (
        "limit_req_zone $binary_remote_addr "
        "zone=search_agent_owner_api:10m rate=30r/m;" in http_config
    )
    assert (
        "limit_req_zone $binary_remote_addr "
        "zone=search_agent_public_analysis:10m rate=2r/m;" in http_config
    )
    assert (
        "limit_conn_zone $server_name "
        "zone=search_agent_public_analysis_concurrency:1m;" in http_config
    )
    assert "map $status $search_agent_auth_rate_limited" in http_config
    assert "map $status $search_agent_public_analysis_rejected" in http_config
    assert "~^(400|403|405|409|413|422|429|500|503)$ 1;" in http_config
    assert "log_format search_agent_auth_limit escape=json" in http_config
    assert (
        "log_format search_agent_public_analysis_rejection escape=json" in http_config
    )
    for forbidden in ("$remote_user", "$http_authorization", "$request_body", "$args"):
        assert forbidden not in http_config
    for safe_field in (
        '"timestamp"',
        '"request_id"',
        '"status"',
        '"method"',
        '"uri"',
        '"request_time"',
    ):
        assert safe_field in http_config
    assert "/var/log/nginx/search-agent-auth-rate-limit.log" in deployment
    assert "/var/log/nginx/search-agent-public-analysis-rejection.log" in deployment
    assert "/etc/logrotate.d/nginx" in deployment
    assert "duplicate log entry" in deployment
    assert "logrotate --debug /etc/logrotate.conf" in deployment


def test_normal_release_installs_http_zones_before_server_locations_and_reload() -> (
    None
):
    deployment = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    normal_release = deployment.split("Normal releases", maxsplit=1)[1]

    rate_config = normal_release.index("/etc/nginx/conf.d/search-agent-rate-limit.conf")
    exact_locations = normal_release.index(
        "synchronize every exact `location = ...` block"
    )
    validate = normal_release.index("sudo nginx -t", exact_locations)
    reload_nginx = normal_release.index("sudo systemctl reload nginx", validate)

    assert rate_config < exact_locations < validate < reload_nginx


def test_public_strategy_catalog_is_exact_and_unknown_agent_routes_fail_closed() -> (
    None
):
    nginx = (ROOT / "deploy/nginx-search-eval.conf").read_text(encoding="utf-8")
    catalog = _nginx_location(nginx, "= /search-eval-api/agent/strategy/catalog")
    fallback = _nginx_location(nginx, "^~ /search-eval-api/agent/")
    bare_agent = _nginx_location(nginx, "= /search-eval-api/agent")

    assert "access_log off;" in catalog
    assert "auth_basic off;" in catalog
    assert "auth_basic_user_file" not in catalog
    assert "proxy_pass http://127.0.0.1:8010/agent/strategy/catalog;" in catalog
    assert 'proxy_set_header Authorization "";' in catalog
    assert "access_log off;" in fallback
    assert "auth_basic off;" in fallback
    assert "proxy_pass" not in fallback
    assert "return 404;" in fallback
    assert "access_log off;" in bare_agent
    assert "auth_basic off;" in bare_agent
    assert "proxy_pass" not in bare_agent
    assert "return 404;" in bare_agent


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
    assert "EnvironmentFile=/etc/search-engine-eva-agent-revision.env" in service
    assert f"Environment=SEARCH_AGENT_ARTIFACT_ROOT={runtime}" in service
    assert "Environment=SEARCH_LOG_LEVEL_AGENT_OPTIMIZATION=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_AGENT_EVAL=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_BAD_CASE=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_BAD_CASE_SUPERVISOR=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_BAD_CASE_WORKER=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_DIAGNOSTIC_EXPERIMENTS=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_HUMAN_ORACLE=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_RETRIEVAL=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_RETRIEVAL_ANALYSIS=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_STAGE_DIAGNOSIS=INFO" in service
    assert "Environment=SEARCH_LOG_LEVEL_QUERY_CONSTRUCTOR=INFO" in service
    assert "ProtectSystem=strict" in service
    assert "KillMode=control-group" in service
    assert "SendSIGKILL=yes" in service
    assert "TimeoutStopSec=10s" in service
    assert "ReadOnlyPaths=/var/www/search-engine-eva-agent" in service
    assert f"ReadWritePaths={runtime}" in service
    assert "UMask=0027" in service
    assert f"-o www-data -g www-data -m 0750 {runtime}" in deployment
    assert "SEARCH_CODE_REVISION=" in deployment
    assert "SEARCH_HUMAN_ORACLE_ALLOWED_ORIGIN=https://shawspace.cn" in deployment
    assert "SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY=" in deployment
    assert "SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY_ID=" in deployment
    assert "SEARCH_HUMAN_ORACLE_OWNER_HMAC_SHA256=" in deployment
    assert "/etc/search-engine-eva-agent-revision.env" in deployment
    assert "sudo systemctl daemon-reload" in deployment
