# Full-catalog portfolio deployment

This deployment serves the baseline search over all 1,814,924 official ESCI
products. It does not claim Amazon production parity or full-catalog relevance
quality; the optimized website lane remains closed. The repository now also
contains a stage-aware retrieval analysis route and its Nginx protection, but
this document does not claim that new route or UI is currently deployed.

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
ls -lh data/index/catalog-baseline-v1.sqlite3
shasum -a 256 data/index/catalog-baseline-v1.sqlite3
```

Record the printed index ID, product count, locale counts, file size and hash.
The build logs can be isolated with:

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_CATALOG=INFO \
  make catalog-index 2>catalog-build.jsonl
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

Add all location blocks from `deploy/nginx-search-eval.conf` inside the existing
HTTPS server block. Before reloading Nginx, create the server-only credential
file interactively; never put the password or generated hash in Git:

```bash
sudo htpasswd -cB /etc/nginx/.search-agent.htpasswd shaw
sudo chown root:www-data /etc/nginx/.search-agent.htpasswd
sudo chmod 0640 /etc/nginx/.search-agent.htpasswd
sudo -u www-data test -r /etc/nginx/.search-agent.htpasswd
sudo nginx -t
sudo systemctl reload nginx
```

The protected locations use the fixed Basic Auth realm `Search Agent Owner`.
This realm intentionally differs from the earlier prototype realm `Search
Agent`, so browsers discard stale cached credentials and present a fresh login
challenge after this release. Keep the realm identical across the page and all
owner-only API locations. Change it again only as an explicit cache-reset
operation because every change forces owners to authenticate again.

Only `/search-agent.html`, the proposal endpoint, the stage-aware
`/agent/retrieval/analyze` endpoint, Agent Eval, the Query constructor, the
59-Query Bad Case endpoint, the diagnostic experiment planner and all seven
Human Diagnostic Oracle endpoints require this credential. The search
experience, strategy page, catalog search and approved strategy catalog stay
public. The exact decision location deliberately returns 404 even with
credentials; human decisions are owner-only and must use the loopback API.
The retrieval location has a 130-second read timeout because its bounded
Runtime policy allows at most 120 seconds. This is a synchronous smoke-only
bridge; before larger data or concurrent use, replace it with a queued worker,
pollable task status and force-terminable job deadline rather than extending the
HTTP timeout again.
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
Oracle identity material:

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
sudo install -d -o www-data -g www-data -m 0750 /var/lib/search-engine-eva-agent/runtime
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install --no-build-isolation --no-deps -e /var/www/search-engine-eva-agent
sudo install -o root -g root -m 0644 \
  /var/www/search-engine-eva-agent/deploy/search-engine-eva-agent.service \
  /etc/systemd/system/search-engine-eva-agent.service
sudo systemd-analyze verify /etc/systemd/system/search-engine-eva-agent.service
sudo systemctl daemon-reload
sudo systemctl restart search-engine-eva-agent
sudo systemctl is-active search-engine-eva-agent
curl http://127.0.0.1:8010/health
curl --request POST 'https://shawspace.cn/search-eval-api/catalog/search' \
  --header 'Content-Type: application/json' \
  --data '{"query":"wireless mouse","top_k":3}'
curl --output /dev/null --write-out '%{http_code}\n' \
  'https://shawspace.cn/search-agent.html'
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
curl --user shaw --request POST \
  'https://shawspace.cn/search-eval-api/agent/retrieval/analyze' \
  --header 'Content-Type: application/json' \
  --data '{"profile":"smoke"}'
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
3. Anonymous requests to the Agent page, proposal endpoint, stage-aware
   retrieval/Bad Case, diagnostic-plan and Human Oracle endpoints return `401`;
   authenticated proposal requests return a
   pending proposal with baseline Run ID, candidate Run ID, comparison ID,
   aggregate metric deltas and bad-case examples. An authenticated retrieval
   analysis returns all three bounded candidate outcomes, stage metrics, 12 gate
   checks and representative product evidence. A successful Bad Case response
   reports exactly 59 calls, zero operational failures, no quality metrics or
   strategy writes, no more than 12 evidence-linked display samples, and a
   validated supervisor receipt containing the 125-second deadline policy and
   TERM/KILL grace. An authenticated, exact-origin Oracle create request returns
   20 clusters/40 candidates/30 intent tasks; missing origin, cross-site or
   non-JSON requests fail closed. Search and strategy pages remain anonymously
   accessible.
4. The strategy catalog endpoint returns the current approved runtime strategy
   list. It can be empty before the Owner approves a proposal.
5. The public decision check returns `404`; only a deliberate loopback request
   from the server can approve or reject a proposal.
6. The source checkout remains clean and unwritable to `www-data`, while only
   the dedicated runtime directory is writable.
7. The website renders full-catalog results in the left lane.
8. The optimized lane is still visibly unsupported.
9. A failed Query can be correlated by `X-Request-ID` without Query text in logs.

For stage-aware retrieval, a successful authenticated response means only that
the local smoke analysis completed. It must report `proposal_ready` or a bounded
terminal status without creating a strategy decision, catalog entry or active
configuration. Verify `/catalog/search` is unchanged. Deployment of this new
route/UI requires an explicit release action and must not be inferred from a
local test or this reference configuration.

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
