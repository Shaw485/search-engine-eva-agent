# ADR-010: Use a dedicated fixed Volcengine Agent Plan provider

- Status: Adopted as an adapter; real custom-backend calls blocked pending
  provider usage-policy confirmation
- Date: 2026-08-29
- Deciders: Owner (provider selection), Codex (adapter, data-boundary and
  operational design)

## Context

ADR-009 introduced a bounded LLM Planner behind the same Runtime, Harness,
Trace and Replay boundaries as the deterministic control. The first real call
was made through the OpenAI provider and failed authentication because that
provider did not accept the supplied credential. The Owner then explicitly
selected **Volcengine Agent Plan** for the next provider integration.

Several products expose APIs that resemble OpenAI request shapes. Treating that
similarity as permission to accept any runtime `base_url` would turn one
reviewed outbound integration into a general server-side network destination.
It would also make provider identity, credential selection, logging and Replay
provenance ambiguous. Converting the Planner contract to Chat Completions would
add another translation layer even though this integration exposes a Responses
path.

The existing privacy boundary also remains important. The authenticated local
analysis response may contain `changed_query_examples`, including Query text,
product IDs/titles and before/after Top 10 results for the Owner's review. Those
local display examples are not part of the aggregate LLM Observation and must
not cross the provider boundary.

## Decision

Add `volcengine_agent_plan` as a dedicated, explicitly selected LLM provider.
Its base URL is compiled into the provider adapter as:

```text
https://ark.cn-beijing.volces.com/api/plan/v3
```

The adapter calls the Responses resource below that fixed base URL
(`https://ark.cn-beijing.volces.com/api/plan/v3/responses`). No environment
variable, API parameter, browser field or task payload may replace the base
URL. The provider/model/credential are selected independently and explicitly:

```text
SEARCH_AGENT_PLANNER=llm
SEARCH_LLM_PROVIDER=volcengine_agent_plan
SEARCH_LLM_MODEL=<explicit reviewed model ID>
SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY=<server-side secret>
```

There is no default model and no cross-provider Key fallback. Missing,
conflicting or invalid configuration fails closed before the first search Tool.
The Key is accepted only from the server process environment (or a stronger
host secret facility), inherited by one disposable provider worker and never
stored in Git, frontend code, API bodies, URLs, logs, Trace or Run evidence.

Volcengine Agent Plan implements the existing provider-independent decision
contract. The Key is transmitted only as authentication to the fixed provider
endpoint. The call also carries the explicit model, fixed system instruction,
strict one-function schema and output limit. For Volcengine, deep thinking is
explicitly disabled: the model selects one ID from a finite allowlist, and the
Runtime must retain latency budget for local experiments and later loop
decisions. Its evidence input is limited to:

- the current finite allowlist of server-generated `option_id` values;
- bounded step/tool counts and the fixed smoke objective/profile;
- aggregate stage metric deltas, gate outcomes, failed-gate names and risk
  rates from already validated Observations.

The model input does **not** contain raw Query text, Query/product identifiers,
titles, ranked results, recovered-product examples, `changed_query_examples`,
artifact paths, evidence references, tool arguments, credentials or free-form
prior reasoning. The server continues to map the selected `option_id` to a
canonical Tool action or finish decision. Runtime owns permissions and budgets;
Harness owns search-quality facts; the Owner retains approval and activation
authority.

## Options considered

### Option A: Arbitrary OpenAI-compatible `base_url`

| Dimension | Assessment |
|---|---|
| Complexity | Low initial code, high security and support burden |
| Cost | Unbounded provider-specific operational variance |
| Scalability | Broad but uncontrolled |
| Team familiarity | Appears familiar while hiding provider differences |

**Pros:** one generic configuration could point at many nominally compatible
services.

**Cons:** creates an SSRF-like outbound destination control, weakens provider
provenance, enables accidental Key exfiltration to the wrong host and makes a
single tested request shape look universally supported.

**Decision:** rejected. This project does not expose a configurable provider
base URL.

### Option B: Dedicated fixed Volcengine Agent Plan provider

| Dimension | Assessment |
|---|---|
| Complexity | Medium; one reviewed adapter and credential namespace |
| Cost | Explicitly attributable to the selected provider/model |
| Scalability | Additive provider adapters behind one stable contract |
| Team familiarity | Clear operational ownership and failure semantics |

**Pros:** fixed outbound authority, unambiguous provider identity, isolated
credential, consistent Runtime/Trace contract and independently testable error
mapping.

**Cons:** provider-specific maintenance and tests; another adapter is required
for each future provider.

**Decision:** adopted.

### Option C: Translate the loop to Chat Completions

| Dimension | Assessment |
|---|---|
| Complexity | Medium to high translation and validation layer |
| Cost | Similar provider spend plus maintenance cost |
| Scalability | Adds a second protocol surface |
| Team familiarity | Familiar API shape but different response semantics |

**Pros:** could reuse older tool-call examples and clients.

**Cons:** unnecessary protocol translation, more malformed-output cases, less
direct parity with the selected Responses integration and a larger Replay/test
matrix.

**Decision:** rejected for this provider. A future Chat Completions adapter
would require its own ADR and exact typed-contract tests.

## Trade-off analysis

The fixed adapter sacrifices generic endpoint configurability to preserve a
small, reviewable network and credential boundary. This is deliberate: the
Agent gains a second model provider without gaining a general HTTP tool. Using
the same aggregate decision contract keeps provider choice operational rather
than granting either provider more evidence or execution authority.

## Consequences

### Positive

- The Owner-selected provider can drive the existing observe-decide-act loop.
- Provider choice, model identity, usage and stable failures remain explicit in
  status, logs and Trace.
- A Volcengine Key cannot silently be retried against OpenAI, or vice versa.
- Query/product display evidence stays in the authenticated local workbench.
- The deterministic control and OpenAI adapter remain available for comparison.

### Negative

- Provider compatibility is implemented and tested separately rather than
  assumed from a shared SDK/request style.
- Operators must configure the provider-specific Key and explicit model.
- Provider availability, latency, model behavior and billing remain external
  dependencies and require a real smoke test.

### Risks

- The fixed integration uses the dedicated Agent Plan key and plan-compatible
  model set with strict provider boundaries. Keep endpoint/key isolation: plan
  calls use `/api/plan/v3` (and `/api/plan/v3/responses`) while regular Ark
  calls use `/api/v3` with a separate normal Ark key.
- The fixed Agent Plan API contract can change; an incompatible response must
  fail closed rather than fall back or loosen validation.
- The model may make a poor legal choice; Harness and Owner approval still
  prevent that choice from becoming an unsupported quality claim or activation.
- Local `changed_query_examples` are sensitive evidence even though they are
  excluded from the model projection; API authentication and artifact access
  controls remain necessary.

## Action items

1. [x] Implement the fixed provider adapter and exact configuration validation.
2. [x] Add offline fake-transport tests for request projection, endpoint lock,
   strict option selection, safe error mapping and secret redaction.
3. [ ] Complete one real-key local smoke against the dedicated Agent Plan
   entrypoint. On 2026-08-30 the first attempt hit
   the 40-second worker deadline; after disabling deep thinking, a diagnostic
   retry distinguished HTTP 401 authentication failure from HTTP 403
   permission rejection. Every attempt stopped before a Tool call. The Agent
   Plan console subsequently confirmed the subscription was active, the fixed
   Base URL was correct, the configured credential type was a dedicated Agent
   Plan API Key, and `doubao-seed-2.1-turbo` was an available model while the
   launcher's former `doubao-seed-2.0-pro` value was not listed. The launcher
   therefore pins `doubao-seed-2.1-turbo`. If the exact integration is
   validated, record the
   successful retry's provider/model, bounded usage, status and stable outcome
   without storing request/response content or the Key.
4. [ ] Compare deterministic, OpenAI and Volcengine Planner task success,
   variance, Tokens, latency and cost before changing the deployed
   deterministic Planner default.
5. [ ] Revisit this ADR if Volcengine removes the Responses path or the project
   needs a different provider/protocol; do not add a generic `base_url` escape
   hatch.
