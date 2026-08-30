# Full-catalog portfolio deployment

This deployment serves an immutable baseline over all 1,814,924 official ESCI
products plus a separately approved active retrieval lane. It does not claim
Amazon production parity or full-catalog relevance quality. The optimized lane
stays closed until an Owner-approved Proposal passes serving validation and the
atomic active pointer names a compatible v2 index revision.

## Topology

```text
shawspace.cn/search-eval.html
          |
          v
Nginx /search-eval-api/* (same HTTPS origin, access log off)
          |
          v
Uvicorn 127.0.0.1:8010 (www-data)
          |-- read-only --> /var/lib/search-engine-eva-agent/catalog-baseline-v1.sqlite3
          |-- read-only --> /var/lib/search-engine-eva-agent/catalog-active-v2.sqlite3
          |
          `-- read/write -> /var/lib/search-engine-eva-agent/runtime/
```

The source Parquet and built index are not stored in Git. Build the verified
index on a trusted machine with enough disk, transfer that single artifact to a
temporary server path, verify its SHA-256, and install it read-only for the
service. The API refuses missing or incompatible metadata.

## Build the artifact

The repository must be clean because its full commit SHA enters the index
identity:

```bash
make data-download
make data-esci-validate
make catalog-index
make catalog-index-v2
ls -lh data/index/catalog-baseline-v1.sqlite3
ls -lh data/index/catalog-v2.sqlite3
shasum -a 256 data/index/catalog-baseline-v1.sqlite3
shasum -a 256 data/index/catalog-v2.sqlite3
```

Record the printed index ID, product count, locale counts, file size and hash.
The build logs can be isolated with:

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_CATALOG=INFO \
  make catalog-index 2>catalog-build.jsonl
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_CATALOG_INDEX=INFO \
  make catalog-index-v2 2>catalog-v2-build.jsonl
```

## First application install

Run these commands on the server after reviewing the paths:

```bash
sudo git clone https://github.com/Shaw485/search-engine-eva-agent.git /var/www/search-engine-eva-agent
sudo python3 -m venv /var/www/search-engine-eva-agent/.venv
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install -r /var/www/search-engine-eva-agent/requirements-dev.lock
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install --no-build-isolation --no-deps -e /var/www/search-engine-eva-agent
sudo install -d -o root -g www-data -m 0750 /var/lib/search-engine-eva-agent
sudo install -d -o www-data -g www-data -m 0750 /var/lib/search-engine-eva-agent/runtime
test -z "$(sudo git -C /var/www/search-engine-eva-agent status --porcelain)"
code_revision="$(sudo git -C /var/www/search-engine-eva-agent rev-parse HEAD)"
printf '%s\n' "$code_revision" | grep -Eq '^[0-9a-f]{40}$'
printf 'SEARCH_CODE_REVISION=%s\n' "$code_revision" | sudo tee /etc/search-engine-eva-agent-revision.env >/dev/null
oracle_actor_hmac_key="$(openssl rand -hex 32)"
oracle_owner_principal=shaw
oracle_owner_hmac_sha256="$(printf '%s' "$oracle_owner_principal" | openssl dgst -sha256 -hmac "$oracle_actor_hmac_key" -r | awk '{print $1}')"
printf 'SEARCH_HUMAN_ORACLE_ALLOWED_ORIGIN=https://shawspace.cn\nSEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY=%s\nSEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY_ID=owner-basic-auth-v1\nSEARCH_HUMAN_ORACLE_OWNER_HMAC_SHA256=%s\n' "$oracle_actor_hmac_key" "$oracle_owner_hmac_sha256" | sudo tee /etc/search-engine-eva-agent.env >/dev/null
unset oracle_actor_hmac_key oracle_owner_hmac_sha256 oracle_owner_principal
sudo chown root:root /etc/search-engine-eva-agent.env /etc/search-engine-eva-agent-revision.env
sudo chmod 0600 /etc/search-engine-eva-agent.env /etc/search-engine-eva-agent-revision.env
sudo cp /var/www/search-engine-eva-agent/deploy/search-engine-eva-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Install the HTTP-scoped rate-limit definitions and add all location blocks from
`deploy/nginx-search-eval.conf` inside the existing HTTPS server block. Before
reloading Nginx, create the server-only credential file interactively; never put
the password or generated hash in Git or a shell argument:

```bash
sudo htpasswd -cB /etc/nginx/.search-agent.htpasswd shaw
sudo chown root:www-data /etc/nginx/.search-agent.htpasswd
sudo chmod 0640 /etc/nginx/.search-agent.htpasswd
sudo -u www-data test -r /etc/nginx/.search-agent.htpasswd
sudo install -o root -g root -m 0644 \
  /var/www/search-engine-eva-agent/deploy/nginx-search-agent-rate-limit-http.conf \
  /etc/nginx/conf.d/search-agent-rate-limit.conf
sudo nginx -t
sudo systemctl reload nginx
```

If an Owner password was ever pasted into chat, a ticket, a command argument or
another non-secret channel, rotate it before enabling the protected Owner APIs:

```bash
sudo htpasswd -B /etc/nginx/.search-agent.htpasswd shaw
```

Enter the replacement only at the interactive prompt. Use a new, high-entropy,
non-reused password; do not pass it on the command line or record it in a log.

## Owner-only optional LLM Planner configuration

The deployed default is the deterministic control and needs no model Key. To
enable the bounded LLM loop for the **Owner-only** analysis route with the
Owner-selected Volcengine Agent Plan provider, edit the existing root-owned
environment file interactively and add these variable names with an explicitly
reviewed model:

```text
SEARCH_AGENT_PLANNER=llm
SEARCH_LLM_PROVIDER=volcengine_agent_plan
SEARCH_LLM_MODEL=<explicit reviewed Agent Plan model ID>
SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY=<server-side secret>
SEARCH_LLM_TIMEOUT_MS=30000
SEARCH_LLM_MAX_OUTPUT_TOKENS=128
```

This is a separate, explicit activation procedure, not a normal code-deployment
step. The current code release must remain deterministic: do not enter, copy,
rotate or activate an Agent Plan Key during deployment, and do not change
`SEARCH_AGENT_PLANNER` to `llm`. Before restarting the service, verify only the
non-secret Planner mode; an absent value means the deterministic default:

```bash
search_planner_mode="$(sudo sed -n 's/^SEARCH_AGENT_PLANNER=//p' \
  /etc/search-engine-eva-agent.env | tail -n 1)"
test -z "$search_planner_mode" || test "$search_planner_mode" = deterministic
unset search_planner_mode
```

An Owner may perform LLM activation later as a separate change after reviewing
provider/model identity, spend and outbound aggregate-data policy. That change
requires its own smoke, rollback evidence and audit record.

The Volcengine adapter has the fixed base URL
`https://ark.cn-beijing.volces.com/api/plan/v3` and calls
`https://ark.cn-beijing.volces.com/api/plan/v3/responses`. Do not add or expose
a configurable `base_url`. For an OpenAI run,
select `SEARCH_LLM_PROVIDER=openai` and use `SEARCH_OPENAI_API_KEY`; do not put a
Volcengine Key in the OpenAI variable or rely on provider fallback. The legacy
`SEARCH_LLM_API_KEY` is accepted only by the OpenAI compatibility path and
should not be used for new deployments.

Do not put the real Key in Git, the systemd unit, shell arguments, browser
JavaScript, Nginx, chat, tickets or deployment output. Keep
`/etc/search-engine-eva-agent.env` owned by `root:root` with mode `0600` and
restart the service after editing. A host-native secret manager or systemd
credential is preferable when available. The selected provider-specific Key is
passed only to a fresh killable provider worker for one model decision; it is
not returned by the status route or written to Trace/logs. Provider, model and
Key configuration must be explicit; there is no default model or arbitrary
endpoint override.

To disable model use without deleting the retained secret immediately, set
`SEARCH_AGENT_PLANNER=deterministic` and restart. This prevents new model calls
because only the Owner route resolves Planner configuration, while the public
route is hard-wired to deterministic mode. Rotate the Key if it has ever
appeared in a non-secret channel.

The Key is sent only as authentication to the fixed provider endpoint. The
model's evidence input contains only the finite allowed option IDs and
aggregate Observation fields: stage metric deltas, gate outcomes/failed-gate
names, bounded risk rates and step/tool budgets. It does not contain Query text,
product IDs/titles, result lists, recovered products or
`changed_query_examples`. The public projected retrieval response may still
return up to ten changed examples from the committed public ESCI smoke fixture;
those examples must not be copied into provider model input, logs or Trace.

`/search-agent.html` is public and `no-store`. The one exact public compute
route, `POST /search-eval-api/agent/retrieval/analyze`, accepts only the fixed
`{"profile":"smoke"}` contract. The application always constructs
`ObservationDrivenRetrievalPlanner` for this route; it never reads
`SEARCH_AGENT_PLANNER`, provider/model configuration or a provider Key. The
response is a strict public v1 projection of committed public ESCI comparison
evidence with provider/model/Token metadata removed. It does not accept
arbitrary Query text, dataset paths, strategy writes or human judgments.

Nginx limits the public route to 2 requests/minute per source IP with a burst of
1, one in-flight request across the whole site, a 1 KiB request body and a
130-second read timeout. It explicitly disables Basic Auth and clears
Authorization, Cookie and Owner-principal headers. The API adds a non-blocking
process-level single-flight lock: an uncached concurrent request returns `409`.
A completed response is cached in memory by exact
`(profile, SEARCH_CODE_REVISION)` and copied on every read, so refreshes do not
repeat the bounded experiment. The cache is lost on service restart and a new
revision uses a new key.

The separate exact route,
`POST /search-eval-api/agent/retrieval/analyze-owner`, is protected by the
`Search Agent Owner` Basic Auth realm. Only this route loads the configured
deterministic or LLM Planner and returns the complete private v2 Runtime
response. It never reads from or writes to the public response cache, so an
authenticated call is a new bounded run. The public and Owner routes share one
process single-flight lock; either returns `409` if the other analysis already
owns it. Nginx strips Basic credentials before proxying, and the provider Key
remains only in the root-owned service environment. The Owner-only
`GET /search-eval-api/agent/retrieval/status` reports configuration readiness
without returning a Key or Prompt.

The auth probe, configured analysis/status routes and every other Owner
data-bearing Agent API location continue to use the fixed Basic Auth realm
`Search Agent Owner`. The probe is limited to 5
requests/minute per source IP with a burst of 3; Owner APIs are limited to 30
requests/minute with a burst of 15. Their `401` result is internally converted to
a final `403`, so the page can handle rejection instead of navigating into a
native 401 flow. Some Nginx builds retain a `WWW-Authenticate` response header
after an `error_page` redirect; the required acceptance signal is the final 403
status and the target browser remaining on the HTML form. Nginx validates every
request and clears the Authorization header before proxying. Normal requests do
not enter an access log. Only rejections are written to the dedicated safe JSON
logs described below. Unknown `/search-eval-api/agent/*` routes fail closed with
`404`; only the exact retrieval analysis and adopted strategy catalog routes are
public. Proposal, Agent Eval, Query construction, Bad Case, diagnostic planning
and Human Oracle routes remain Owner-authenticated. The exact decision location
deliberately returns 404 even with credentials; human decisions are owner-only
and must use the loopback API.

Every Basic-auth location sends only `crit` events to
`/var/log/nginx/search-agent-auth-critical.log`. This prevents routine unknown
user/password-mismatch events—which can contain the submitted username in
Nginx—from entering production logs while preserving critical Nginx failures.
Rate-limit responses alone are written to
`/var/log/nginx/search-agent-auth-rate-limit.log` as structured JSON containing
timestamp, request ID, source IP, status, method, path and duration. It never
contains username, Authorization, request body or query parameters. The module
can be debugged independently with
`sudo tail -f /var/log/nginx/search-agent-auth-rate-limit.log`; filter with
`jq 'select(.status == 429)'`. Reuse the host's existing
`/etc/logrotate.d/nginx` wildcard for rotation; do not add a second stanza that
matches this file, because logrotate treats that as a `duplicate log entry`.
Before reload, confirm the wildcard covers `/var/log/nginx/*.log` and run
`sudo logrotate --debug /etc/logrotate.conf`. Retention, compression and reopen
semantics then remain identical to the other Nginx logs. Alert on a sustained
non-zero 429 rate or repeated bursts from one source IP. Verbose auth failure
logging stays disabled in production.

Public analysis rejections use a separate structured JSON file,
`/var/log/nginx/search-agent-public-analysis-rejection.log`. It contains only
timestamp, request ID, source IP, status, method, exact path and duration; it
does not contain request bodies, Query arguments, usernames, cookies,
Authorization or Owner principal headers. View only this module with
`sudo tail -f /var/log/nginx/search-agent-public-analysis-rejection.log` and
filter busy/limit responses with `jq 'select(.status == 409 or .status == 429)'`.
The same existing `/etc/logrotate.d/nginx` wildcard must cover this `.log` file;
verify that once with `sudo logrotate --debug /etc/logrotate.conf` and do not
add a duplicate matching stanza. Accepted requests do not enter this Nginx
access log. API public-cache miss/hit/busy events and Owner-run
busy/failure events are separately available through the `api` journal module
and contain only the fixed profile, stable error metadata and trace identifiers,
never response content. Owner analyses never emit a public-cache hit or store
event.

The retrieval location has a 130-second read timeout because its bounded
Runtime policy allows at most 120 seconds. This remains a synchronous smoke-only
bridge protected by Nginx concurrency and the API single-flight lock; before
larger data or multi-worker use, replace it with a queued worker, pollable task
status and force-terminable job deadline rather than extending the HTTP timeout
again. If Uvicorn is ever configured with multiple workers, replace the
process-local lock/cache with a shared store before deployment.
Agent Eval uses the same 130-second proxy timeout. Bad Case diagnostics uses
140 seconds for its 125-second process-group deadline plus bounded TERM/KILL
grace and HTTP overhead. Human Oracle behavior view uses 40 seconds because the
server replays and verifies one complete cluster; the Query constructor,
diagnostic planner and other Oracle routes use 15 seconds. All exact owner
routes clear the Nginx Basic Auth header, disable request access logs and send
`Cache-Control: no-store`. Oracle routes also overwrite the internal principal
header from Nginx authentication; the browser cannot choose its actor identity.
The Agent Eval runner refuses new work after its private artifact tree exceeds
2 GiB. Monitor `/var/lib/search-engine-eva-agent/runtime/agent-evals/`; archive
old execution receipts and Traces according to the host retention policy while
retaining any deterministic evidence cited by a decision or incident.
Bad Case artifacts use a separate 256 MiB tree watermark plus a 2 GiB free-space
preflight. Monitor
`/var/lib/search-engine-eva-agent/runtime/bad-case-diagnostics/`; failed
capacity preflight does not write another attempt receipt.
Human Oracle artifacts use a separate 64 MiB tree watermark. Monitor
`/var/lib/search-engine-eva-agent/runtime/human-oracle/`; keep this directory
private because it is the append-only audit record for Owner judgments, even
though raw Query and product content are excluded.

The Oracle actor HMAC key, key ID and allowlisted Owner HMAC in
`/etc/search-engine-eva-agent.env` are server-only identity material. Do not
expose them to the browser, logs, Git or deployment output. The allowlist digest
must be derived from the exact Basic Auth username Nginx places in
`$remote_user`; a second valid htpasswd user is still denied by the application.
Changing the key, key ID or allowlist changes the pseudonymous actor identity,
so do not rotate them while a review batch is open. The allowed origin must
exactly match the HTTPS site origin.

## Install or replace the index

Transfer the artifact to a new, explicit temporary file on the server. Compare
its SHA-256 with the recorded local value before installation. Then:

```bash
sudo install -o root -g www-data -m 0640 \
  /tmp/catalog-baseline-v1.sqlite3.upload \
  /var/lib/search-engine-eva-agent/catalog-baseline-v1.sqlite3.new
sudo mv /var/lib/search-engine-eva-agent/catalog-baseline-v1.sqlite3.new \
  /var/lib/search-engine-eva-agent/catalog-baseline-v1.sqlite3
sudo systemctl enable --now search-engine-eva-agent
```

The fixed target path is deliberate; the source path and expected hash must be
resolved before these replacement commands are run. The old artifact is
replaced atomically on the same filesystem. Keep the local verified artifact so
the previous index can be reinstalled if verification fails.

Build or transfer the full-field v2 artifact separately. On the 1.9 GiB host,
the builder streams bounded Parquet batches and requires at least 30 GiB free
for source, SQLite temporary state and both installed indexes. A server-side
build tied to the deployed clean commit is:

```bash
cd /var/www/search-engine-eva-agent
sudo ./scripts/download_esci.sh
test -z "$(sudo git status --porcelain)"
sudo /usr/bin/time -v .venv/bin/python -m search_quality.catalog.v2_cli \
  --source data/raw/esci/shopping_queries_dataset_products.parquet \
  --lock data/esci.lock.json \
  --output /var/lib/search-engine-eva-agent/catalog-active-v2.sqlite3.new \
  --batch-size 10000 \
  --log-module catalog_index=INFO
sudo chown root:www-data \
  /var/lib/search-engine-eva-agent/catalog-active-v2.sqlite3.new
sudo chmod 0640 \
  /var/lib/search-engine-eva-agent/catalog-active-v2.sqlite3.new
sudo mv /var/lib/search-engine-eva-agent/catalog-active-v2.sqlite3.new \
  /var/lib/search-engine-eva-agent/catalog-active-v2.sqlite3
sudo -u www-data test -r \
  /var/lib/search-engine-eva-agent/catalog-active-v2.sqlite3
```

Installing this file does not activate a strategy. Only the authenticated
release decision route can validate an approved Proposal and advance
`runtime/retrieval-strategies/active.json`. Keep the v1 file unchanged: it is
the comparison lane and the first rollback target.

## Update and verify

Before the first update from a pre-Oracle installation, migrate the old
revision-only environment file exactly once. This generates a persistent actor
key; do not repeat this block for normal releases or while a review batch is
open:

```bash
oracle_actor_hmac_key="$(openssl rand -hex 32)"
oracle_owner_principal=shaw
oracle_owner_hmac_sha256="$(printf '%s' "$oracle_owner_principal" | openssl dgst -sha256 -hmac "$oracle_actor_hmac_key" -r | awk '{print $1}')"
printf 'SEARCH_HUMAN_ORACLE_ALLOWED_ORIGIN=https://shawspace.cn\nSEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY=%s\nSEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY_ID=owner-basic-auth-v1\nSEARCH_HUMAN_ORACLE_OWNER_HMAC_SHA256=%s\n' "$oracle_actor_hmac_key" "$oracle_owner_hmac_sha256" | sudo tee /etc/search-engine-eva-agent.env >/dev/null
unset oracle_actor_hmac_key oracle_owner_hmac_sha256 oracle_owner_principal
sudo chown root:root /etc/search-engine-eva-agent.env
sudo chmod 0600 /etc/search-engine-eva-agent.env
```

Normal releases update only the separate code-revision file and preserve the
Oracle identity material. After pulling the release, install the new
HTTP-scoped rate definitions **before** synchronizing the exact public and
Owner locations into the HTTPS server. Validate the combined configuration
with `nginx -t` before reloading; reversing this order can leave a newly-added
location referring to a rate-limit zone that Nginx has not loaded yet:

```bash
sudo git -C /var/www/search-engine-eva-agent pull --ff-only
test -z "$(sudo git -C /var/www/search-engine-eva-agent status --porcelain)"
code_revision="$(sudo git -C /var/www/search-engine-eva-agent rev-parse HEAD)"
printf '%s\n' "$code_revision" | grep -Eq '^[0-9a-f]{40}$'
printf 'SEARCH_CODE_REVISION=%s\n' "$code_revision" | sudo tee /etc/search-engine-eva-agent-revision.env >/dev/null
sudo chown root:root /etc/search-engine-eva-agent-revision.env
sudo chmod 0600 /etc/search-engine-eva-agent-revision.env
sudo test -s /etc/search-engine-eva-agent.env
sudo grep -Eq '^SEARCH_HUMAN_ORACLE_ALLOWED_ORIGIN=https://shawspace\.cn$' /etc/search-engine-eva-agent.env
sudo grep -Eq '^SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY=[0-9a-f]{64}$' /etc/search-engine-eva-agent.env
sudo grep -Eq '^SEARCH_HUMAN_ORACLE_ACTOR_HMAC_KEY_ID=[a-z][a-z0-9-]{0,63}$' /etc/search-engine-eva-agent.env
sudo grep -Eq '^SEARCH_HUMAN_ORACLE_OWNER_HMAC_SHA256=[0-9a-f]{64}$' /etc/search-engine-eva-agent.env
# Code deployment does not activate a provider Key. An absent Planner value is
# deterministic; an explicit value must also remain deterministic for this release.
search_planner_mode="$(sudo sed -n 's/^SEARCH_AGENT_PLANNER=//p' \
  /etc/search-engine-eva-agent.env | tail -n 1)"
test -z "$search_planner_mode" || test "$search_planner_mode" = deterministic
unset search_planner_mode
sudo install -d -o www-data -g www-data -m 0750 /var/lib/search-engine-eva-agent/runtime
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install -r /var/www/search-engine-eva-agent/requirements-dev.lock
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install --no-build-isolation --no-deps -e /var/www/search-engine-eva-agent
sudo /var/www/search-engine-eva-agent/.venv/bin/pip check
sudo install -o root -g root -m 0644 \
  /var/www/search-engine-eva-agent/deploy/search-engine-eva-agent.service \
  /etc/systemd/system/search-engine-eva-agent.service
sudo systemd-analyze verify /etc/systemd/system/search-engine-eva-agent.service
sudo systemctl daemon-reload
sudo systemctl restart search-engine-eva-agent
sudo systemctl is-active search-engine-eva-agent
curl http://127.0.0.1:8010/health
sudo install -o root -g root -m 0644 \
  /var/www/search-engine-eva-agent/deploy/nginx-search-agent-rate-limit-http.conf \
  /etc/nginx/conf.d/search-agent-rate-limit.conf
# Now synchronize every exact `location = ...` block from
# deploy/nginx-search-eval.conf into the existing shawspace.cn HTTPS server.
sudoedit /etc/nginx/sites-enabled/shawspace.cn
sudo nginx -t
sudo systemctl reload nginx
curl --request POST 'https://shawspace.cn/search-eval-api/catalog/search' \
  --header 'Content-Type: application/json' \
  --data '{"query":"wireless mouse","top_k":3}'
curl --output /dev/null --write-out '%{http_code}\n' \
  'https://shawspace.cn/search-agent.html'
curl --output /dev/null --write-out '%{http_code}\n' \
  'https://shawspace.cn/search-agent-auth-check.json'
curl --user 'invalid-test-owner:invalid-test-only' \
  --dump-header - --output /dev/null \
  'https://shawspace.cn/search-agent-auth-check.json'
curl --user shaw \
  'https://shawspace.cn/search-agent-auth-check.json'
curl --output /dev/null --write-out '%{http_code}\n' \
  'https://shawspace.cn/search-eval-api/agent/unknown-route'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST 'https://shawspace.cn/search-eval-api/agent/strategy/propose' \
  --header 'Content-Type: application/json' \
  --data '{"profile":"smoke"}'
curl --user shaw --request POST \
  'https://shawspace.cn/search-eval-api/agent/strategy/propose' \
  --header 'Content-Type: application/json' \
  --data '{"profile":"smoke"}'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST \
  'https://shawspace.cn/search-eval-api/agent/retrieval/analyze' \
  --header 'Content-Type: application/json' \
  --data '{"profile":"smoke"}'
# The public payload is v1 and contains no configured Planner/provider usage.
curl --request POST \
  'https://shawspace.cn/search-eval-api/agent/retrieval/analyze' \
  --header 'Content-Type: application/json' \
  --data '{"profile":"smoke"}' \
  | jq -e '.agent_run.schema_version == "retrieval-agent-run-summary-v1" and (.agent_run | [has("planner_mode"), has("provider_id"), has("model_id"), has("llm_usage")] | any | not)'
# The configurable full-v2 route and readiness route stay Owner-only.
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST \
  'https://shawspace.cn/search-eval-api/agent/retrieval/analyze-owner' \
  --header 'Content-Type: application/json' \
  --data '{"profile":"smoke"}'
curl --user shaw --request POST \
  'https://shawspace.cn/search-eval-api/agent/retrieval/analyze-owner' \
  --header 'Content-Type: application/json' \
  --data '{"profile":"smoke"}' \
  | jq -e '.agent_run.schema_version == "retrieval-agent-run-summary-v2" and .agent_run.planner_mode == "deterministic"'
curl --user shaw \
  'https://shawspace.cn/search-eval-api/agent/retrieval/status' \
  | jq -e '.planner_mode == "deterministic" and .state == "deterministic" and (.model_id == null) and (.provider_id == null)'
# Non-POST methods must be rejected by the exact public location.
curl --output /dev/null --write-out '%{http_code}\n' \
  'https://shawspace.cn/search-eval-api/agent/retrieval/analyze'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST 'https://shawspace.cn/search-eval-api/agent/eval/run' \
  --header 'Content-Type: application/json' \
  --data '{"suite":"stage5-retrieval-v1"}'
curl --user shaw --request POST \
  'https://shawspace.cn/search-eval-api/agent/eval/run' \
  --header 'Content-Type: application/json' \
  --data '{"suite":"stage5-retrieval-v1"}'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST \
  'https://shawspace.cn/search-eval-api/agent/query-constructor/build' \
  --header 'Content-Type: application/json' \
  --data '{"source":"smoke"}'
curl --user shaw --request POST \
  'https://shawspace.cn/search-eval-api/agent/query-constructor/build' \
  --header 'Content-Type: application/json' \
  --data '{"source":"smoke"}'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST \
  'https://shawspace.cn/search-eval-api/agent/bad-cases/run' \
  --header 'Content-Type: application/json' \
  --data '{"source":"smoke"}'
curl --user shaw --request POST \
  'https://shawspace.cn/search-eval-api/agent/bad-cases/run' \
  --header 'Content-Type: application/json' \
  --data '{"source":"smoke"}'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST \
  'https://shawspace.cn/search-eval-api/agent/diagnostic-experiments/plan' \
  --header 'Content-Type: application/json' \
  --data '{"diagnostic_id":"bad-case-000000000000","query_set_id":"query-set-000000000000"}'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST \
  'https://shawspace.cn/search-eval-api/agent/human-oracle/batches/status' \
  --header 'Content-Type: application/json' \
  --data '{"oracle_batch_id":"oracle-batch-000000000000"}'
# Replace the IDs below with IDs returned by the authenticated Bad Case run.
curl --user shaw --request POST \
  'https://shawspace.cn/search-eval-api/agent/human-oracle/batches/create' \
  --header 'Content-Type: application/json' \
  --header 'Origin: https://shawspace.cn' \
  --header 'Sec-Fetch-Site: same-origin' \
  --data '{"diagnostic_id":"bad-case-REPLACE_ME","query_set_id":"query-set-REPLACE_ME"}'
curl 'https://shawspace.cn/search-eval-api/agent/strategy/catalog'
curl --output /dev/null --write-out '%{http_code}\n' \
  --request POST 'https://shawspace.cn/search-eval-api/agent/strategy/decision' \
  --header 'Content-Type: application/json' \
  --data '{"proposal_id":"proposal-000000000000","decision":"reject"}'
sudo -u www-data test -w /var/lib/search-engine-eva-agent/runtime
sudo -u www-data test ! -w /var/www/search-engine-eva-agent
test -z "$(sudo git -C /var/www/search-engine-eva-agent status --porcelain)"
```

Acceptance requires:

1. Health reports `catalog.status=ready`, the expected index ID and 1,814,924 products.
2. English, Spanish, Japanese and exact product-ID checks return valid JSON.
3. Anonymous requests to the Agent page return `200` and the read-only workbench
   is usable without a password. The fixed smoke retrieval analysis returns
   `200` (or `409` while its single flight is busy), always identifies the
   public v1 deterministic Runtime, and contains no Planner mode, provider,
   model or Token usage fields. Anonymous calls to the Owner analysis/status,
   auth check, proposal, Agent Eval, Query constructor, Bad Case,
   diagnostic-plan and Human Oracle endpoints return a final `403`; the target
   browser must not show a native authentication error. Unknown Agent routes
   return `404`. The
   authenticated auth check returns only the fixed schema and boolean,
   the authenticated Owner analysis returns the full v2 deterministic Runtime
   for this release without a public-cache event, and authenticated proposal
   requests return a
   pending proposal with baseline Run ID, candidate Run ID, comparison ID,
   aggregate metric deltas and bad-case examples. The public retrieval analysis
   returns all three bounded candidate outcomes, stage metrics, 12 gate
   checks and representative product evidence. A successful Bad Case response
   reports exactly 59 calls, zero operational failures, no quality metrics or
   strategy writes, no more than 12 evidence-linked display samples, and a
   validated supervisor receipt containing the 125-second deadline policy and
   TERM/KILL grace. An authenticated, exact-origin Oracle create request returns
   20 clusters/40 candidates/30 intent tasks; missing origin, cross-site or
   non-JSON requests fail closed. No credential appears in Cookie, URL,
   `localStorage`, `sessionStorage`, browser logs, Nginx rejection logs or
   application logs. Search and strategy pages remain anonymously accessible.
4. The strategy catalog endpoint returns the current approved runtime strategy
   list. It can be empty before the Owner approves a proposal.
5. The public decision check returns `404`; only a deliberate loopback request
   from the server can approve or reject a proposal.
6. The source checkout remains clean and unwritable to `www-data`, while only
   the dedicated runtime directory is writable.
7. The website renders full-catalog results in the left lane.
8. The optimized lane is still visibly unsupported.
9. A failed Query can be correlated by `X-Request-ID` without Query text in logs.

For stage-aware retrieval, a successful public response means only that the
deterministic smoke analysis completed. It must report `proposal_ready` or a
bounded terminal status without creating a strategy decision, catalog entry or
active configuration. Verify `/catalog/search` is unchanged. A successful
Owner response proves only that the configured Planner completed the same
bounded workflow; it does not approve or activate its candidate. Enabling an
LLM/provider Key is a separate Owner change and must not be inferred from this
code deployment, a public run or a previous local smoke.

Proposal, retrieval Run/diagnosis/comparison and decision JSON under the
runtime directory are evidence artifacts rather than logs. They can contain
Query and product examples. Keep
the directory private (`0750`), monitor its size, back it up only when evidence
must be retained, and review artifacts before export. Do not delete a proposal
that has an associated human decision.

Bad Case deterministic evidence under `bad-case-diagnostics/evidence/` stores
only hashes and aggregate behavior; execution and failed-attempt receipts store
only IDs, counts, stages and timing. The source Query-set artifact remains
private because it contains raw Query text. The owner-only HTTP display sample
is not a durable evidence artifact and must not be cached or copied into logs.
The parent supervisor writes a separate immutable receipt under
`bad-case-diagnostics/supervisor-executions/`; only that receipt can prove that
the child completed before the hard deadline.

Human Oracle batches, append-only annotations and seals under `human-oracle/`
store hashes, constrained judgments and pseudonymous actor IDs, but no raw
Query, title, product ID or result list. Transient intent and behavior views
contain Owner-visible evidence and must never be copied into browser storage,
analytics, access logs or exports. A sealed Oracle is diagnostic evidence only:
it creates no product relevance labels, quality conclusion, root-cause claim or
strategy write.

To record an intentional human decision, sign in to the server and call
`http://127.0.0.1:8010/agent/strategy/decision` directly with the reviewed
proposal ID. This mutates the strategy catalog, so it is not part of automated
deployment verification.

Use a response request ID to inspect safe diagnostics:

```bash
sudo journalctl -u search-engine-eva-agent --since '15 minutes ago' -o cat \
  | jq -R 'fromjson? | select(.trace_id == "TRACE_ID")'
```

Uvicorn and the Nginx location disable request-line access logs. Public search
uses POST so Query text stays out of the URL, browser history and proxy request
lines. Module controls, redaction and journald retention are documented in
[LOGGING.md](LOGGING.md).
