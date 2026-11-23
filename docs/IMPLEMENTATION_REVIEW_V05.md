# v0.5 implementation and verification review

## Completed changes

- Added process-owned bounded PostgreSQL pools with explicit checkout/waiter
  limits, direct-connection comparison mode, rollback and fork ownership checks.
- Replaced the workflow-wide global bottleneck with tenant/run row locking and
  eligible-run `FOR UPDATE SKIP LOCKED`. A shared legacy advisory-lock bridge
  coordinates new workers with old exclusive-lock workers and schema/admission
  operations during rollout.
- Read operations use read-only repeatable snapshots and do not take the writer
  lock. Every state-changing workflow path follows run-before-child lock order.
- Model budgets and settlement remain atomic across concurrent calls. Claims,
  retry limits, durable deadlines, separate approval, cancellation and stale
  lease fencing retain regression coverage.
- Owned execution mutations check deadlines after acquiring database locks.
  Late completion, retry, checkpoint, expansion, heartbeat or artifact attachment
  records a terminal deadline without publishing progress. The worker checks
  elapsed time after synchronous execution; incurred usage can still settle.
- Added an explicit Inkling 2 Mini preset, dedicated Tinker credential loading,
  named/numeric reasoning effort validation and output limits. Text-only gateway
  requests reject truncation, tool-call/refusal/unknown terminal statuses and
  reasoning-only replies. Reported usage survives rejected content.
- One configurable elapsed model-call deadline includes connection, headers,
  streamed body and fallback routes. It stops slow dribbling and retains
  uncertain reservations; body size and compression checks remain enforced.
- Added the provider and pool configuration to Compose/.env/readme. Added a
  reproducible Store comparison harness and expanded the actual PostgreSQL CI
  job to include concurrent scheduler invariants.

## Verification

The exact machine-readable record is [testing-results-v05.json](testing-results-v05.json).
The final core run executed **500 cases: 230 unit + 270 integration**, with zero
skips. Its **16 external cases were explicitly deselected**, not counted as passes.
The PostgreSQL selection executed **14 cases with no skips**, covering eleven new
pool/concurrency cases, two existing 64-bit-accounting/migration cases, and the
existing real workflow/fencing contract. The real database was PostgreSQL 17.12.

Core line coverage is **90.05%**, branch coverage **82.93%** across all **23**
production Python files. Existing gates remain **85%/75%**; no omissions or
exclusions were added. Both repository Ruff gates, `git diff --check`, and
`uv lock --check` passed. The test runner emitted an upstream Starlette/httpx
compatibility deprecation warning; it did not skip or fail tests.

Independent review identified and corrected additional gaps before final evidence:
foreign-tenant run-lock contention, explicit abnormal model termination reasons,
rolling compatibility with v0.4's exclusive advisory lock, and late completion
after a database lock wait. Deterministic SQLite and real PostgreSQL contention
regressions cover the last issue; local TCP tests cover dribbling and stalled
model responses. A huge integer reasoning parameter also now produces a normal
validation error.

## Independent process acceptance

The final local PostgreSQL + Redis selection passed **15/15** cases with no
skips. Separate real-process checks passed **6/6 on SQLite**, **6/6 on
PostgreSQL**, and **7/7 for the RAG/pgvector lifecycle**. They exercise actual
RAG HTTP, the Agent API, independent workers and MCP; kill/restart recovery,
stale-result fencing after cancellation, SSE cursor reconnection, bounded
retry/deadline behavior, revision and tenant isolation, feature-disabled
rejection, and historical purge. The model responses in these acceptance
checks are explicitly synthetic/offline.

Local Chromium could not create required Unix sockets, and the built MinIO
server could not enumerate host addresses because this sandbox prohibits
netlink. These are recorded as local execution limits, never passes. The
existing actual browser, Docker Compose, Redis/S3 and research-model CI jobs
remain enabled against the pinned companion RAG revision.

## Measured storage result

[Raw measurements](../reports/postgres-comparison-v05.json) compare baseline
`830a27123f3d64266f15ae96706ffd249f3cf5d9` to the production-source fingerprint in
the report. Each revision/concurrency level ran **80 samples × 3 repetitions**
plus three unmeasured warmups per workload, using its own process and owned
schema. Revision order alternated between repetitions. All **2,880 measured
operations** passed result checks. The report preserves every repetition and
checks that production source did not change while measuring.

Values below are medians across repetitions. A claim + complete sample includes
both transitions for one synthetic arithmetic job. It does not execute a model
or send HTTP traffic. The upgraded process used its default pool cap of four.

| Operation | Concurrency | Baseline p95 ms | Current p95 ms | Baseline ops/s | Current ops/s | Throughput ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Workflow read | 1 | 5.78 | 0.96 | 282.0 | 1780.9 | 6.32× |
| Claim + complete | 1 | 10.28 | 3.23 | 113.9 | 453.5 | 3.98× |
| Workflow read | 4 | 10.03 | 4.54 | 522.3 | 1242.0 | 2.38× |
| Claim + complete | 4 | 25.30 | 19.17 | 175.0 | 306.0 | 1.75× |
| Workflow read | 8 | 17.08 | 9.70 | 518.8 | 1173.0 | 2.26× |
| Claim + complete | 8 | 52.20 | 32.44 | 163.7 | 328.3 | 2.01× |

Connection reuse and reduced lock contention explain the measured improvement;
this small shared-host experiment does not establish production capacity,
linear scaling, fairness, or high availability. Higher concurrency does not
necessarily improve throughput on this host. Repeat on deployment hardware
with representative job mixes before sizing the worker fleet.

## Remaining evidence boundary

No live Inkling request was made without an authorized provider credential.
The HTTP protocol tests use explicitly identified fixtures. Storage throughput
above is **not** an Inkling latency/quality result. Cross-service/browser and
optional Redis/S3 checks are separate CI/evidence layers; this review does not
substitute the core suite for them. Frozen RAG admission quality is unchanged.

## Tested companion revision

All six companion checkout jobs are pinned to RAGs v0.9 commit
`30a2e2bcec11120f92b8219d9ce5f71e2792c270`. The final local SQLite/PostgreSQL
process checks were repeated after the durable deadline fix, with the same
6/6, 6/6 and 7/7 successful outcomes. The final real PostgreSQL + Redis
selection includes 14 PostgreSQL cases and one Redis case, with zero skips.
