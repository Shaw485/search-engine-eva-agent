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
printf 'SEARCH_CODE_REVISION=%s\n' "$code_revision" | sudo tee /etc/search-engine-eva-agent.env >/dev/null
sudo chown root:root /etc/search-engine-eva-agent.env
sudo chmod 0600 /etc/search-engine-eva-agent.env
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

Only `/search-agent.html`, the proposal endpoint, the stage-aware
`/agent/retrieval/analyze` endpoint, Agent Eval and the Query constructor
require this credential. The search
experience, strategy page, catalog search and approved strategy catalog stay
public. The exact decision location deliberately returns 404 even with
credentials; human decisions are owner-only and must use the loopback API.
The retrieval location has a 130-second read timeout because its bounded
Runtime policy allows at most 120 seconds. This is a synchronous smoke-only
bridge; before larger data or concurrent use, replace it with a queued worker,
pollable task status and force-terminable job deadline rather than extending the
HTTP timeout again.
Agent Eval uses the same 130-second proxy timeout for its fixed 12-task Suite;
the Query constructor uses 15 seconds. Both exact routes clear the Nginx Basic
Auth header before proxying and disable request access logs.
The Agent Eval runner refuses new work after its private artifact tree exceeds
2 GiB. Monitor `/var/lib/search-engine-eva-agent/runtime/agent-evals/`; archive
old execution receipts and Traces according to the host retention policy while
retaining any deterministic evidence cited by a decision or incident.

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

```bash
sudo git -C /var/www/search-engine-eva-agent pull --ff-only
test -z "$(sudo git -C /var/www/search-engine-eva-agent status --porcelain)"
code_revision="$(sudo git -C /var/www/search-engine-eva-agent rev-parse HEAD)"
printf '%s\n' "$code_revision" | grep -Eq '^[0-9a-f]{40}$'
printf 'SEARCH_CODE_REVISION=%s\n' "$code_revision" | sudo tee /etc/search-engine-eva-agent.env >/dev/null
sudo chown root:root /etc/search-engine-eva-agent.env
sudo chmod 0600 /etc/search-engine-eva-agent.env
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
3. Anonymous requests to the Agent page, proposal endpoint and stage-aware
   retrieval endpoint return `401`; authenticated proposal requests return a
   pending proposal with baseline Run ID, candidate Run ID, comparison ID,
   aggregate metric deltas and bad-case examples. An authenticated retrieval
   analysis returns all three bounded candidate outcomes, stage metrics, 12 gate
   checks and representative product evidence. Search and strategy pages remain
   anonymously accessible.
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
