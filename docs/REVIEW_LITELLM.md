# Whole-codebase review remediation and shared gateway

This change implements the execution-plane findings from the 2026-11-23 review
of `0412d065bd1ea8bacd38574c2704f48ea423acb5`, together with the optional shared
LiteLLM deployment. The seven companion knowledge-plane fixes are described in
[RAGs](https://github.com/MasterLKH180cm/RAGs/blob/master/docs/REVIEW_LITELLM.md).
The complete design contracts are [REVIEW_EXECUTION_FIX_SPEC.md](REVIEW_EXECUTION_FIX_SPEC.md)
and [LITELLM_SPEC.md](LITELLM_SPEC.md).

## Corrected behavior

| Review finding | Change | Regression evidence |
| --- | --- | --- |
| CR-05: aggregate usage bypassed namespace restrictions | Apply tenant and namespace policy in SQL before selecting records or totals; exclude unbound usage for restricted readers | SQLite and actual PostgreSQL aggregate, per-run, legacy default and foreign-tenant cases |
| CR-08: synchronous storage delayed unrelated lease renewal | Bounded offload for API/worker/model persistence; a separate heartbeat pool; completion fencing checks the local execution deadline after locks | Actual PostgreSQL row-lock contention, delayed completion/accounting, cancellation and bounded admission cases |
| CR-10: API and MCP disagreed on the Inkling provider | Share the resolved provider contract across configuration, API and MCP | Complete provider matrix including `inkling` and explicit `routed` capabilities |

Ordinary synchronous work has 16 worker threads and 64 admitted operations;
lease renewal has a separate 16-thread/16-slot pool. Admission follows the
underlying operation, including queued/running work after waiter cancellation.
Overload is explicit, and durable lease/attempt recovery remains authoritative.
Independent executors do not create additional database capacity: exhausting a
shared connection pool can still delay operations. Uncertain model usage stays
conservatively reserved until bounded background settlement commits.

## Shared generation deployment

The optional Compose profile pins LiteLLM 1.110.2 by OCI digest and uses a
separate PostgreSQL database, verified HTTPS and distinct model/route-restricted
service keys. The application environments remain independent of the isolated
hash-locked LiteLLM test environment. No gateway is enabled by the ordinary
default stack, and no model is downloaded by bootstrap.

The generation alias is `gateway/inkling-2-mini-iq4nl-v1`. It has one configured
upstream, no cache, no fallback and zero proxy/provider-SDK retries. Workflow
authorization, approval, cancellation, lease fencing and per-run usage remain
application responsibilities. Embeddings retain their own explicit identity.

Key initialization verifies active status, expiry and the exact model/route
policy. It refuses deleted/revoked keys and changed provisioning receipts;
startup does not restore revoked access. Prompt/response and error-body logging
are disabled. Private CA trust uses regular bounded nonsymlink files and normal
certificate/hostname checks. See [LITELLM.md](LITELLM.md) for setup, key isolation,
image signature verification, certificate rotation and direct/proxy comparison.

## Validation evidence

- [testing-results-review-litellm.json](testing-results-review-litellm.json)
  records local unit/integration inventory, actual PostgreSQL/Redis contracts,
  all-production-module coverage and source hashes. Separate two-service
  PostgreSQL and SQLite process-loss/recovery checks are included without
  adding their subprocess execution to parent-process coverage.
- [litellm-process-acceptance.json](../reports/litellm-process-acceptance.json)
  records real proxy processes, PostgreSQL, verified TLS, production application
  adapters, native RAG agent behavior, key restrictions, usage, truncation,
  deadline and upstream-attempt tests. Its upstream responses are synthetic.
- [testing-image-verification.json](testing-image-verification.json) records
  actual Cosign verification of the pinned image and actual Compose rendering.
  Signature verification and rendering do not imply container execution.
- The [CI workflow](../.github/workflows/ci.yml) pins the exact companion RAG
  commit, reruns the actual proxy harness, starts the full container stack and
  probes both application containers through the signed gateway image. Existing
  browser, PostgreSQL, Redis, S3, MCP and process-recovery gates remain active.

The development host has no Docker engine. Local MinIO startup is blocked by
host netlink permissions, so its one S3 contract is explicitly deselected
locally and retained in CI. Raw provider logs and generated credentials are
not committed or included in evidence uploads. The development pass did not
deploy to an existing environment or change user databases.

## Model performance boundary

No actual Inkling inference was executed. The selected GGUF weights exceed
available host RAM and this host has no GPU. The gateway contract fixtures do
not establish answer quality, tokens per second or production capacity.
On a suitable inference host, run the direct and proxy benchmarks against the
same weights/runtime/quantization/context/slots/threads manifest. The RAG
comparison rejects missing/malformed runtime evidence and source-hash mismatch.
Nonstreaming measurements do not report TTFT or decoder-only throughput.
