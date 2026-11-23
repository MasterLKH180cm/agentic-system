# Execution review correction contract

This change corrects CR-05, CR-08 and CR-10 from the 2026-11-23 whole-code
review. It does not change the at-least-once execution guarantee or authorize
new tools, scopes, providers or effects.

Status: implemented. The [review report](REVIEW_LITELLM.md) maps the corrections
to regression coverage, and [local results](testing-results-review-litellm.json)
record the tested source snapshot separately from published CI results.

## Usage scope

`GET /usage` must apply the authenticated principal's workflow namespace
restriction to both the returned calls and every aggregate. A restricted
principal sees only usage bound to runs in its allowed namespaces. Unbound
usage has no namespace ownership and is visible only to an unrestricted
tenant principal. An empty namespace allowlist sees no calls. Tenant filtering
is mandatory even for joins. Explicit forbidden run IDs retain their 403
response. Older specifications without a memory namespace use `default`, as
the existing workflow authorization contract does.

## Nonblocking persistence

Synchronous database and storage operations invoked from async API, tool,
runtime and model paths must run outside the event loop. A process-owned
executor admits only a finite number of calls; cancellation cannot release
admission capacity until the underlying operation has actually stopped. The
heartbeat lane has separate executor capacity so a blocked ordinary write
cannot prevent the coroutine responsible for lease renewal from running.

Database operations retain their finite connection/statement/lock waits and
their transaction-local lease, cancellation and deadline checks. Cancelling
an await does not cancel an already running database transaction. In particular,
late completion, checkpoint or artifact attachment must still fail fencing.
Usage reservations remain conservative if cancellation occurs before a
reservation result is available; provider requests cannot begin without the
reservation result. Settlement remains first-write-wins and cannot erase an
already incurred charge.

The ordinary executor has 16 threads and 64 total admitted calls; the renewal
executor has 16 threads and 16 admitted calls. Queued calls retain admission
even when their asyncio waiter is cancelled, preventing cancelled queue items
from bypassing the bound. A model timeout/cancellation schedules bounded
settlement and returns without waiting for its database lock. The conservative
reservation remains visible until settlement commits. Under admission overload
it remains reserved; this can overestimate spend but cannot erase it.

A worker supplies its stricter local deadline to every owned mutation in its
job context. The store checks that deadline after acquiring the database lock,
so an offloaded completion cannot succeed after a cancelled wait even if the
persisted original step deadline is later.

The monitor covers both tool execution and durable finalization. Independent
work must keep making progress while another workflow waits for a PostgreSQL
row lock. This is bounded concurrency, not a promise of progress when the
database itself is unavailable or every configured connection is occupied.
Admission overload is explicit and cannot create an unbounded executor queue.

## Provider capability contract

The API's resolved provider identity and MCP validation share one supported
set: `offline`, `openai`, `anthropic`, `inkling`, `routed`. `inkling` is a
configured model preset using OpenAI-compatible transport; it is not a new
wire protocol. The environment selector retains its existing accepted values;
explicit routes continue to resolve `offline` to `routed`.

## Private gateway trust

An optional `ca_file` on a model route loads a regular nonsymlink PEM certificate bundle
of at most 1 MiB. It requires HTTPS, keeps hostname/certificate verification
enabled, and disables ambient proxy/environment trust for that route. Other
routes retain their existing trust policy. This configures transport trust;
it does not attest to the model artifact loaded behind a gateway alias.

## Acceptance

- Exercise the actual API and store with authorized, forbidden, unbound,
  cross-tenant and legacy-default usage; check totals as well as records.
- Feed actual API capabilities from every accepted configuration to MCP and
  check tool registration, including Inkling and routed local Inkling.
- Hold one actual PostgreSQL run row beyond a short supported lease and show
  an unrelated workflow completes and a slow unrelated workflow renews its
  lease. Verify no replay or stale attachment becomes accepted.
- Verify bounded executor admission under cancellation and heartbeats under
  ordinary executor saturation; do not use sleep-only mocks as the database
  acceptance result.
- Run the existing worker, deadline, budget, synthesis, model-failure,
  namespace, MCP and PostgreSQL regression suites.
