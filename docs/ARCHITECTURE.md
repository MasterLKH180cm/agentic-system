# Agentic Workflow System design

## Split between repositories

- RAGs: evidence ingestion, indexed search, context and grounded citations.
- agentic-system: API/control, DAG validation, durable workflow records, worker
  leases, dependencies, tool execution, role-gated approvals and event audit.

## Real execution lifecycle

1. Authenticated client POSTs /workflows with a goal or explicit validated DAG.
2. Workflow is atomically persisted, with optional idempotency key.
3. Worker transactionally claims a ready step with a lease and unique fencing token.
4. Independent ready steps are executed in parallel through asyncio.gather.
5. A tool performs a bounded operation against a configured trusted endpoint.
6. Success commits output and events; failure schedules bounded backoff retries.
7. Steps with effectful internal artifact publish require a separate approver token.
8. Expired leases are reclaimed; stale workers cannot overwrite newer completions.
9. State and events persist across API/worker process restarts.

## Security boundaries

- API tokens are mapped server-side to tenants. No tenant can select another tenant.
- Approval uses a **different** token-to-tenant mapping, never the requester's token.
- Supported tools are allowlisted and validated. No arbitrary shell, SQL or URL tool.
- URL for RAGs is set by the operator, never by an agent-supplied workflow argument.
- Numerical expressions are interpreted with AST allowlisting, never eval().
- Retrieved data is untrusted, not an instruction for the workflow/tool executor.
- Model-proposed plans must pass the identical DAG/tool validation as user plans.
- Logs record metadata rather than raw secret-bearing prompts or tool responses.
- In v0.1, internal publish writes ONLY the workflow output to the local store.
  There is no unapproved external email/post/payment tool.

## Important limitations

- The worker provides practical **at-least-once**, bounded execution with a
  lease and fencing. External side effects still require per-tool idempotency.
- SQLite WAL is suitable for one shared host/volume; NOT horizontally shared
  over arbitrary multi-node network filesystems.
- There is no full Temporal history replay, scheduler fairness, task priority,
  distributed rate limiting or durable external message queue in v0.1.
- No arbitrary browser or filesystem agent. No general multi-agent framework.
- Human approval is protected by a separate key; production must additionally
  enforce human identity, role and audit via OIDC / an identity gateway.
- Worker attempts and state are visible via HTTP, but no SSE push stream yet.
- Model-provider failures lead to bounded retries, not fabricated success.

## Production migration

For large or high-availability workloads, use Temporal/managed queues with
PostgreSQL. Put nondeterministic LLM/tool calls into retryable Activities;
do not put arbitrary network I/O inside deterministic workflow replay.
Add OIDC, RBAC/ABAC, quotas, TLS, per-tenant budget accounting, HA metrics,
secret management and distributed OpenTelemetry before public deployment.
