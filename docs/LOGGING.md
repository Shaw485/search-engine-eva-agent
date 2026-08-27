# Logging and independent diagnostics

The project separates immutable experiment evidence from runtime diagnostics:

- Stage manifests and Run JSON are the reproducible source of truth.
- Logs explain one execution and may contain timing or failure information.
- A random `trace_id` belongs only in logs; it never enters deterministic Run
  identity or metric calculations.

## Modules and defaults

All events use the `search_quality.<module>` logger namespace and are emitted to
stderr. Normal CLI results remain on stdout.

| Module | Covers | Production default |
|---|---|---|
| `api` | HTTP request boundary and public failure correlation | `INFO` in systemd |
| `backend` | Local/OpenSearch smoke lifecycle | `WARNING` |
| `data` | Stage 1 validation and build lifecycle | `WARNING` |
| `evaluation` | Profile validation, baseline execution and Run storage | `WARNING` |
| `ranking` | Per-Query Ranker diagnostics | `OFF` |

The library default is `WARNING`. Verbose ranking events are opt-in because one
event per Query becomes noisy on larger profiles.

## Enable, disable, and filter

Environment variables accept `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`,
or `OFF`:

```bash
export SEARCH_LOG_FORMAT=json
export SEARCH_LOG_LEVEL=WARNING
export SEARCH_LOG_LEVEL_EVALUATION=INFO
export SEARCH_LOG_LEVEL_RANKING=DEBUG
```

CLI entry points also accept repeatable module overrides. These examples keep
JSON diagnostics separate from the normal result:

```bash
.venv/bin/python -m search_quality.evaluation.cli \
  --profile smoke --ranker all \
  --log-module evaluation=INFO \
  --log-module ranking=DEBUG \
  2>evaluation-debug.jsonl

.venv/bin/python -m search_quality.data.cli --validate-only \
  --log-module data=INFO 2>data-validation.jsonl

.venv/bin/python -m search_quality.smoke \
  --backend local --log-module backend=INFO \
  2>backend-smoke.jsonl
```

To isolate one subsystem, set the global level to `OFF` and enable only that
module:

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_EVALUATION=DEBUG \
  make eval-baseline 2>evaluation-only.jsonl
```

## Event fields

JSON events include:

- UTC timestamp, level, logger, module and stable event name;
- `trace_id` for one CLI execution or HTTP request;
- profile, Ranker, Query ID and candidate counts when relevant;
- Run ID and aggregate metrics after a completed evaluation;
- duration plus stable error code and error type at execution boundaries.

Per-Query debug events use numeric Query IDs and counts. They do not include raw
Query text, product titles, descriptions, vectors, request bodies or result
payloads.

## Privacy and public errors

The formatter normalizes case, punctuation and camelCase before recursively
redacting credential, password, token, secret, authorization, cookie, API key,
private key, generic error text, query text, title, description, vector, payload
or body fields. Free-form log messages are rejected: `event` must be a stable
snake_case identifier. This is a last line of defence; callers still emit only
safe codes and counts, never generic exception text.

The portfolio API returns a generic error code plus `trace_id`. Internal
exception text is not sent to the browser. The preferred search request is POST
with a JSON body, while Uvicorn and Nginx also disable default access logs. The
deprecated GET compatibility endpoint puts Query text in the URL and must not
be used for sensitive searches. The API emits a safe allowlisted route event;
unknown path content is recorded only as `unmatched`.

## Server viewing, filtering, and export

The systemd unit writes JSON events to journald:

```bash
sudo journalctl -u search-engine-eva-agent -n 100 -o cat
sudo journalctl -u search-engine-eva-agent --since '15 minutes ago' -o cat \
  | jq -R 'fromjson? | select(.module == "api")'
sudo journalctl -u search-engine-eva-agent -o cat \
  | jq -R 'fromjson? | select(.trace_id == "TRACE_ID_FROM_ERROR")'
```

Export only the time range and module needed for diagnosis. Review exported
files before sharing even though structured fields are redacted by default.

The application does not create unbounded log files. CLI capture files are
operator-owned and should be deleted after diagnosis. Production retention,
rotation and size limits are controlled centrally by journald settings such as
`SystemMaxUse` and `MaxRetentionSec`; inspect the host policy with:

```bash
systemd-analyze cat-config systemd/journald.conf
```

## Reproduce by subsystem

1. `data`: rerun `--validate-only` with only `data=DEBUG`.
2. `evaluation`: use the fixed smoke profile with `evaluation=DEBUG` and keep
   `ranking=OFF` unless a specific Query needs inspection.
3. `ranking`: enable `ranking=DEBUG`; filter by `query_id`. Raw Query text is in
   the immutable Run evidence rather than duplicated into logs.
4. `backend`: run the ten-product local smoke with `backend=DEBUG`; OpenSearch
   remains an explicitly separate optional integration.
5. `api`: reproduce the request and filter journald using the response
   `X-Request-ID` or error `trace_id`.

`tests/test_observability.py` verifies JSON structure, module isolation,
redaction variants, error classification, stable events, low-noise defaults and
handler de-duplication. API tests verify that successful, backend-failed and
unhandled requests remain traceable without logging Query strings or exposing
internal error causes. Deployment contract tests keep Uvicorn access logging
disabled in both local and systemd entry points.
