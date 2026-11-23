# Shared LiteLLM gateway acceptance contract

Status: implemented, 2026-11-23. The [review report](REVIEW_LITELLM.md),
[local validation record](testing-results-review-litellm.json) and
[process acceptance report](../reports/litellm-process-acceptance.json) document
executed evidence and its limits; they do not assert published CI status or
real-model performance. This change follows the whole
code review of Agentic `0412d06` and RAGs `9813f4c`. Component regression
specifications cover the ten verified defects; this specification defines the
optional shared generation integration and its evidence boundary.

## Responsibilities and compatibility

Agentic retains workflow state, permissions, approvals, leases, cancellation,
idempotency, deadlines and per-run usage reservations. RAGs retains source and
index validity, tenant authorization, retrieval and citations. LiteLLM runs in
its own process and handles approved generation aliases. It does not execute
tools or own either application's database. Existing direct providers and the
credential-free default deployment remain supported.

The first profile exposes one versioned OpenAI-compatible Inkling generation
alias, `gateway/inkling-2-mini-iq4nl-v1`. Its upstream is explicitly configured;
the operator must retain the runtime/model/quantization manifest. Changing that
deployment requires a new alias. Both applications use an explicit `max_tokens`
contract with no reasoning-effort override. Native RAG tool calling and Agentic
JSON planning/synthesis are separate acceptance cases. No generic multimodal or
all-provider reasoning compatibility is claimed.

Embeddings do not use the new gateway profile. RAG router embedding identity
includes the resolved endpoint and optional explicit embedding-space version;
existing profiles with the literal `router` endpoint require reindexing.
Embedding fallback across spaces is never enabled by the generation profile.

## Deployment and trust

Provide an opt-in Compose overlay/profile for the full stack and a standalone
gateway Compose deployment. Pin LiteLLM to the verified stable `1.110.2`
release; retain the upstream release and signing-verification instructions.
No floating `main`, `latest` or `main-stable` tags are allowed.

The gateway has its own PostgreSQL database for virtual keys and spend
accounting. RAGs and Agentic receive separate model-restricted service keys;
neither receives the administrative master key, upstream key or database
credential. The initialization procedure is restart-safe and must not print
secrets. A shared service key does not imply tenant-specific gateway budgets.
Tenant scope and run-level reservation stay enforced by the application.

Use HTTPS with certificate verification. A private installation can configure
an explicit trusted CA file; no client disables verification, trusts ambient
proxy settings for the RAG router, or changes the application's production
environment to permit plaintext. Mount CA and service credentials read-only,
separately from the gateway's server key and administrative credentials.
Configuration generation must fail rather than overwrite existing files.

## Retry, accounting and confidentiality

For the first release, gateway and provider-SDK retries are zero, fallbacks and
cache are disabled, and each alias has one deployment. Agentic remains the
owner of bounded route/job retries; applications retain a cancellable absolute
call deadline. Incomplete usage keeps conservative reservations. Do not assign
a made-up per-token price to local inference; unknown monetary cost remains
unknown. Gateway spend data is supplemental to application accounting.

Do not record prompts, responses, private reasoning or credentials in gateway
spend/error logs. Disable unsolicited external telemetry and debug output.
Configuration, logs, test reports and diagnostics must not expose secrets.

## Acceptance gates

1. All ten review regressions must be fixed, with genuine PostgreSQL coverage
   for migration, source validity and worker lock/heartbeat behavior.
2. Existing unit/integration suites, full-package coverage floors, static
   checks, packaging and actual two-service process smoke remain required.
3. Start the pinned real LiteLLM proxy against a local deterministic HTTP
   provider. Verify authenticated generation through both application adapters,
   native RAG tool calls, Agentic usage, wrong-key/model refusal, truncation,
   total timeout and upstream attempt counts. Such responses remain fixtures.
4. Verify the Compose overlay and service/key/CA boundaries. Exercise the actual
   container stack in CI when the local host has no Docker engine.
5. Provide a repeatable direct-versus-proxy comparison using the same model
   deployment manifest and workload. Report separate transport fixture timings
   and real inference results; no TTFT claim without streaming measurements.
6. Record exact revisions, versions, executed/deselected tests, coverage and
   outstanding hardware or external-service limitations. The known host memory
   limit must not be relabelled as successful Inkling inference.

## Upstream sources

- https://docs.litellm.ai/release_notes/v1.110.2/v1-110-2
- https://docs.litellm.ai/docs/proxy/release_cycle
- https://docs.litellm.ai/docs/proxy/docker_image_security
- https://docs.litellm.ai/docs/routing
- https://docs.litellm.ai/docs/proxy/virtual_keys
- https://docs.litellm.ai/docs/proxy/cost_tracking
- https://docs.litellm.ai/docs/reasoning_content
