# v0.4: operator feature flags and a PostgreSQL/pgvector deployment

Status: specified before implementation, 2026-11-22.
Baseline: merged v0.3 `1bfeae240fa42a6331b0942bda5a3b2a5305c886`.

## Requested outcome and boundaries

Make every existing optional functional capability configurable, document a
complete installation/use path, and deploy the knowledge service on PostgreSQL
with pgvector. Agent workflow state uses PostgreSQL in that same stack but in a
separate database. Agent memory remains explicitly lexical; do not relabel it
as semantic/vector memory. The source of knowledge retrieval is the RAG service.

## Feature configuration

- A central manifest defines boolean permissions for `dag`, `agent_loop`, `ui`,
  `api_docs`, `sse`, `webhooks`, `mcp`, `registry`, `memory`, `artifacts`,
  `approvals`, `rag_search`, `synthesis`, `math`, `merge`, `model_generation`,
  `model_fallback` and `telemetry`. Defaults preserve existing availability;
  provider/backend/credential prerequisites still determine actual capability.
- `AGENT_FEATURES_FILE` optionally names a UTF-8 JSON object of overrides.
  `AGENT_FEATURES_JSON` overrides individual file entries. Unknown keys,
  non-booleans, malformed JSON and unreadable explicitly selected files fail
  startup. Operational settings remain validated environment configuration.
- An authenticated capability endpoint describes effective flags, supported
  modes/tools and storage identity without secret values or DSNs. The UI and
  MCP must not advertise disabled work as executable functionality.
- Gate direct HTTP endpoints, inbound hooks, MCP requests, workflow submission,
  planner-produced steps, automatic memory access and actual worker execution.
  Disabled operations fail explicitly (`feature_disabled`, HTTP 403) before
  provider/network/storage side effects. Disabled UI/docs may be unregistered.
- Snapshot submitted feature constraints with durable workflow specifications;
  execution intersects the snapshot with the current worker configuration.
  Restart/resume cannot bypass a newly disabled feature, and enabling a feature
  later cannot relax an already submitted restriction. Legacy runs retain their
  old declared behavior only to the extent permitted by current configuration.
- Disabling approvals never auto-approves an effectful step: such submissions
  and execution are rejected. Disabling artifact support must avoid initializing
  an S3 client or calling its endpoint. Disabled telemetry initializes no exporter.
- `model_generation` blocks configured online calls without relabeling them as
  offline success. `model_fallback=false` restricts a purpose to its first
  applicable route. Preserve existing route ordering, budget reservations,
  settlement and limited transient fallback behavior.
- Authentication, tenant/namespace policies, input bounds, lease fencing,
  cancellation, execution budgets and required evidence refusal remain mandatory.
  Feature flags are not switches for bypassing these invariants.

## Configuration and deployed database

- Preserve explicit provider/model/endpoint routes and environment/secret-file
  configuration. API and worker must receive the same documented settings.
  Forward advanced options in Compose, including model routes, custom key
  environment references, feature files/maps and policy settings. Document any
  arbitrary custom variable that needs an explicit environment mapping.
- Default Compose uses released `pgvector/pgvector:0.8.7-pg17`, separate
  RAG/Agent databases and non-superuser application roles. Bootstrap prepares
  the vector extension. RAG uses `RAGS_DATABASE_URL`; Agent uses its existing
  `AGENT_DATABASE_URL`. Database ports remain internal, HTTP localhost-only.
- Preserve `AGENT_DB_PATH` as explicit SQLite development compatibility. Do not
  silently move, drop or overwrite an existing SQLite database/volume. Document
  how existing stores differ from a fresh PostgreSQL deployment and how to stop
  workers safely before a planned transition.
- Provide reproducible setup with generated, distinct local credentials,
  non-overwrite behavior, persistent volumes, model/feature configuration,
  authenticated examples, stop/restart, backup and restore guidance. Preserve
  the existing README architecture/flowcharts and current implementation limits.

## Verification and completion

- Unit tests cover strict file/JSON precedence, manifest completeness, malformed
  configuration and route/model behavior. Integration tests use actual APIs,
  persisted runs and independent worker execution to show disabled features
  cannot be reached through another entry point or resumed workflow.
- `RAGS_E2E_POSTGRES_URL` and existing `AGENT_E2E_POSTGRES_URL` select real stores
  explicitly after the child-process environment allowlist. Each E2E harness
  uses its own disposable RAG/Agent databases. Never inherit ambient secrets.
- Execute the real API/worker/MCP and browser flow against the pgvector stack,
  including enabled and disabled feature controls, persisted retrieval,
  tenant/update/as-of/delete boundaries and restart behavior. Report actual
  backend identity so SQLite fallback cannot masquerade as PostgreSQL evidence.
- Retain all existing CI execution gates, strict inventories, no-selected-skip
  enforcement and unchanged full-package coverage floors. Add actual pgvector
  service coverage; do not weaken tests or suppress unmeasured production code.
- Publish a reviewed RAG implementation first, pin every companion checkout to
  its exact commit, then verify Agent CI. After successful review and CI, merge
  both PRs and verify default-branch CI as previously authorized.
- Frozen quality results and disabled-by-default admission profiles remain
  unchanged. No new upstream RAG implementation or model-quality claim is added.
