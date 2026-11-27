# Provider, approval and embedding selection review

This follow-up starts from `732c10728c2f8014388af03a951f73fdbb1c95e5`.
The [specification](PROVIDER_APPROVAL_REVIEW_SPEC.md) was committed before the
implementation. Existing CR-05/08/10 corrections remain in place; the findings
below were reproduced against the current source.

## Corrections

| Finding | Corrected behavior | Regression evidence |
| --- | --- | --- |
| Permissive outer provider JSON could replace usage with duplicate fields or accept nonfinite numbers | One shared JSON parser rejects duplicate keys, exponent overflow, invalid Unicode and excessive nesting; uncertain reservations remain charged | Real TCP provider responses; the same 21 targeted cases fail against the unchanged baseline production package |
| Text could conceal tool-call or malformed Anthropic blocks | Validate response collections, messages and block types; accept only text completion, keeping supported reasoning blocks out of the visible answer; retain well-formed reported usage on rejection | OpenAI tool/function-call mixtures, Anthropic malformed/tool blocks, reasoning plus text, invalid shapes and no-fallback checks |
| Approval values were silently coerced | API decisions and step approval requirements require actual booleans; the store validates again before starting a transaction | HTTP and SQLite checks preserve status, events and worker ineligibility on malformed decisions; true/false remain effective |
| Direct `ModelRoute` construction skipped JSON-path validation | Validate identity, purpose uniqueness, known transport and finite nonnegative prices at construction | Direct-object and environment configuration suites |
| Credential transports inherited ambient proxy/CA settings | Model, RAG and MCP clients explicitly disable environment transport overrides; explicit model-route CA verification remains supported | Actual provider proxy trap and real local RAG/MCP HTTP with invalid ambient proxy/CA values |
| Recovery test assumed cancellation had already released every admitted DB transaction | Await a claim for at most two seconds after the row blocker is released; retain original unrelated-run deadlines and stale-result checks, and assert a new lease token | Actual PostgreSQL row-lock/heartbeat regression |

Provider block validation follows the primary
[OpenAI chat completion contract](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [Anthropic Messages contract](https://platform.claude.com/docs/en/api/messages).
No native tool execution is added to the text completion route.

## Model and tokenizer selection

The full-stack Compose RAG service now forwards model revision, dimensions,
query/document prefixes and input types, authentication mode and custom token
window settings. These settings do not enter the Agent API/worker environment.
The [README selection guide](../README.md#choose-an-embedding-model-and-tokenizer)
shows the RAG model/tokenizer browser, guided configuration, separate credential
file, image extras, container artifact paths and embedding-index compatibility.
Generation budgets retain their existing conservative reservation model.

## Local verification

| Selection | Result |
| --- | ---: |
| Unit tests | 552 passed |
| Ordinary integration tests | 458 passed |
| Actual PostgreSQL tests | 18 passed |
| **Distinct pytest cases executed** | **1,028 passed, zero selected skips** |
| Whole production package coverage | 24 modules; 90.876% lines / 85.201% branches |
| SQLite HTTP/API/worker/stdio MCP lifecycle | 7/7 checks |
| PostgreSQL/pgvector HTTP/API/worker/stdio MCP lifecycle | 7/7 checks |
| PostgreSQL evidence/refusal/merge lifecycle | 3/3 checks |
| Actual standalone Compose rendering | Default and override configurations passed |
| Ruff, whitespace and installed-dependency consistency | Passed |

Coverage is from ordinary parent-process unit/integration execution. External
processes are not folded into that percentage. Existing 85% line / 75% branch
floors, exclusion protections, dependency locks, frozen model measurements and
quality thresholds were not changed.

The local server is **PostgreSQL 16.2 + pgvector 0.8.7**, using TCP-only disposable
clusters and non-superuser database-owner roles. pgvector was built from official
tag commit `f37c13f68b57d2c3472b2214fbcff699d6d34876`. Declared deployment/CI stays
PostgreSQL 17 + pgvector 0.8.7. Standalone Compose 5.8.0 was hash-verified and
actually rendered configuration; there is no local Docker engine. Redis, S3,
containers, research models and browser gates remain CI responsibilities.

Initial failures remain described in the
[machine-readable verification record](testing-results-provider-approval.json):
the bundled pgvector 0.6.2 lacked `l2_normalize(vector)` required by RAG, and one
PostgreSQL run exposed the recovery fixture's instantaneous-claim assumption.
The local environment also initially supplied an unsupported SOCKS proxy to
HTTPX. These attempts are not counted as passes. Final provider transports
explicitly isolate environment proxies; the final core suite passes with the
ordinary host environment present.

There is no new live paid-model inference, model-quality promotion or performance
claim in this change. The Qwen generation/admission failures remain unchanged.
