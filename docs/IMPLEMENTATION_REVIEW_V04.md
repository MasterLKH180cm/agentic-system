# Agentic System v0.4 implementation and review

The [v0.4 specification](SPEC_V04.md) was committed as `944fd23` before implementation.
[PR #6](https://github.com/MasterLKH180cm/agentic-system/pull/6) adds operator feature
controls and PostgreSQL deployment alongside the separate RAG knowledge service.
See also the [testing review](TEST_REVIEW.md).

## Configuration and enforcement

The [manifest](../agentic/features.py) defines eighteen boolean permissions.
`AGENT_FEATURES_FILE` supplies overrides; `AGENT_FEATURES_JSON` overrides individual
file entries. Empty optional values mean unset. Unknown names, duplicate keys,
non-booleans, invalid JSON/UTF-8 and unreadable selected files fail configuration.
Defaults retain existing provider/backend prerequisites. Authenticated `/capabilities`
reports permissions and storage identity without
credentials, endpoints or connection strings.

| Permissions | Enforcement boundary |
| --- | --- |
| `dag`, `agent_loop` | Submission, planner output, worker dispatch and recovered checkpoints |
| `rag_search`, `synthesis`, `math`, `merge` | Plan validation and actual dispatch; disabled tools are marked unavailable |
| `registry`, `memory` | Definitions/references, direct memory endpoints, implicit recall and resumed execution |
| `artifacts`, `approvals` | Publication admission, approval and execution; disabled approvals never authorize effects; disabled artifacts initialize no S3 client |
| `ui`, `api_docs`, `sse`, `webhooks`, `mcp` | HTTP interfaces and dashboard controls; the shipped MCP bridge checks local/remote capabilities and sends a server-enforced interface header |
| `model_generation`, `model_fallback` | Online calls are rejected before reservation/provider work; disabled fallback selects only the first applicable route |
| `telemetry` | Disabled telemetry initializes no exporter; enabling it still requires the existing telemetry configuration |

Disabled operations return HTTP 403 `feature_disabled`; UI/docs return 404.
Workers persist `outcome=policy_denied` and
`stop_reason=feature_disabled`. Cancellation and authorized inspection remain available.

## Durable constraints and mandatory controls

Submissions store `specification.feature_flags`. Execution intersects that snapshot
with current worker permissions: restarts can narrow access; enabling features cannot
relax older restrictions. Legacy runs remain subject to current permissions. Checkpoint
validation precedes returning a cached completed plan.

Authentication, tenant/namespace policy, distinct requester/approver identities,
lease fencing, cancellation, bounded retries, budgets, citation provenance and
required evidence refusal remain mandatory. No flag disables these controls.

## Review and regression evidence

[Configuration](../tests/test_features_config.py),
[API/worker](../tests/test_features_integration.py),
[model](../tests/test_features_model.py) and [MCP](../tests/test_features_mcp.py)
tests exercise rejection and enabled behavior. Local HTTP provider tests
verify that a transient first-route failure retains its durable charge and makes
no fallback request when fallback is disabled. Enabled fallback still reaches the
second provider. Configured online generation never becomes offline success.

Independent cross-review reproduced RAG preflight gaps in captioning, iterative
retrieval and research tokenizer/encoder loading, plus ignored multimodal environment
flags. Corrections reject before provider calls, model loading or index writes;
regressions verify zero calls. These fixtures establish execution contracts without
claiming model quality.

## Deployment boundaries

Compose uses `pgvector/pgvector:0.8.7-pg17`, separate RAG/Agent databases and
roles, internal database networking and consistent API/worker settings.
`AGENT_DATABASE_URL` stores durable execution state; RAG owns actual pgvector
retrieval. Agent memory remains lexical. SQLite remains an explicit compatibility
option; changing a DSN does not migrate data. ANN performance and production failover
remain unverified.

## Verified implementation snapshot

The reviewed implementation `6876ccade1d6c85bf87e86722c58162c5c14f88d` passed
all seven automatic jobs in [CI 44996511159](https://github.com/MasterLKH180cm/agentic-system/actions/runs/44996511159),
using RAG `f3c9506b850c0196517861b11cf51ca56d6ed991`.
Artifacts record **160 unit, 244 ordinary integration and five external-service
tests**, with zero failures, errors or selected skips. Coverage across all
22 production modules is **2,641/2,922 statements (90.38%)** and
**958/1,156 branches (82.87%)**, with zero exclusions and unchanged **85%/75%** floors.

Separate evidence confirms **7/7 pgvector/feature checks**, **12/12 actual Chromium
checks**, and **6/6 resilience checks on each of SQLite and PostgreSQL**. Real
HTTP/worker/MCP chains, RAPTOR, LightRAG, frozen-profile admission and all four
Compose services also passed. Compose uses restrictive `umask 077` during bootstrap
and verifies native vector storage and restart persistence. External processes
are not folded into the parent-process coverage percentage.

[Machine-readable results](testing-results-v04.json) retain artifact digest,
per-file coverage, production SHA-256 hashes and detailed browser/pgvector evidence.
Subsequent documentation and companion-pin updates retain these production source
identities; the PR's current checks certify the complete final revision.
Frozen RAG quality results and default profile admission remain unchanged.
