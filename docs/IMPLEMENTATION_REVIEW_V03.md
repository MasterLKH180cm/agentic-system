# Agentic System v0.3 implementation and review

The [v0.3 specification](SPEC_V03.md) was committed before the corresponding
feature work. This review extends the historical [v0.2 ledger](IMPLEMENTATION_REVIEW.md)
and the README's complete [12-layer mapping](../README.md#12-layer-coverage).
It records a concrete correction to evidence handling, not a claim of a complete
distributed production platform.

All four README flowcharts passed actual Mermaid11.12.1 parsing with jsdom26.1.0;
the [diagram record](diagram-validation-v03.json) identifies their source hashes.
This verifies syntax, not visual browser rendering.

## Why this change was needed

The previous retrieval tool discarded RAG's negative support decision and built
an extractive answer from the returned citations. A static synthesis step could
return a refusal sentence without a failed domain outcome, so its workflow could
appear successful. A valid model refusal without citations could instead become
a generic exception and retry. Runtime evaluation and old checkpoints could
then preserve the wrong result.

The new shared contract separates a tool that executed successfully from a
retrieval that found sufficient evidence. Refusal survives packing, merges,
synthesis, bounded refinement, durable completion and public inspection.

## Requirement evidence

| Gates | Implemented behavior | Evidence |
| --- | --- | --- |
| AG3-T01–T02 | Empty/refused retrieval remains nested domain data; final synthesis persists `insufficient_evidence`, fails the run and prevents dependent publication; refused IDs cannot be revived | [Evidence tests](../tests/test_evidence_v03.py), [synthesis tests](../tests/test_synthesis_v03.py), [real v0.3 E2E](../tests/e2e_v03.py) |
| AG3-T03 | Independent admitted branches remain usable; finite arithmetic zero and exact structured zero retain complete provenance | Evidence/synthesis tests; real RAG structured aggregate over HTTP |
| AG3-T04–T05 | Strict synthesis JSON, fixed refusal reasons, no retry for normal refusal, complete UTF-8 prompt/schema/framing/output accounting | Actual local provider HTTP fixture in synthesis tests; no paid-model quality claim |
| AG3-T06–T07 | Negative assessment, bounded replan, last refusal reason and committed-observation recovery; old active success checkpoints re-evaluate without replay | [Runtime tests](../tests/test_runtime_v02.py), evidence tests and real independent-worker E2E |
| AG3-T08 | Required-policy startup checks and per-run profile snapshots; finite thresholds and approved profiles; separately verified actual pinned scorer boundary | Evidence tests and [actual admission HTTP harness](../tests/e2e_admission_v03.py); RAG owns the frozen quality report |
| AG3-T09 | Actual pinned upstream identity and authorized original source IDs survive RAG→Agent execution | [RAPTOR chain report](https://github.com/MasterLKH180cm/RAGs/blob/feat/spec-driven-v02/reports/research_raptor_http_v03.json), [LightRAG chain report](https://github.com/MasterLKH180cm/RAGs/blob/feat/spec-driven-v02/reports/research_lightrag_http_v03.json) |
| AG3-T10–T11 | Both stdio MCP bridges inspect terminal outcomes; REST/SSE agree with durable state; new events omit query/source/provider explanations | Real v0.3 E2E, actual upstream chains and sentinel checks |

Real HTTP verification starts separate API and worker processes and uses disposable
data. MCP verification parses tool results and polls the actual terminal run;
a handshake or nonempty response is insufficient. SQLite and PostgreSQL share
the same durable-result requirements. The existing external PostgreSQL, Redis,
S3-compatible and Compose gates remain in CI.

## Correctness decisions

`rag.search` stores the validated query assessment under `retrieval`. An empty
branch does not abort another independent branch. `collect_evidence` recursively
quarantines refused sources before collecting usable citations or exact values.
An unknown citation, contradictory typed assessment or malformed provenance
raises an execution error rather than becoming trusted evidence.

Required mode accepts `calibrated_reranker` or `calibrated_answerability` only
with finite score/threshold metadata and a snapshotted allowlisted profile.
Calibrated answerability acceptance requires its explicit `supported=true`.
Relevance-only acceptance may leave support null. Exact structured aggregates
are separately validated and must identify their authorized datasets.

Model-backed synthesis uses one finite JSON envelope. A normal model refusal
returns fixed user-facing text and no usable sources. With no usable evidence,
the gateway makes no reservation or provider request. Complete exact observations
and their provenance are packed before optional source excerpts; they cannot be
silently discarded behind text. Available evidence that cannot fit produces
`context_budget_exhausted`.

The complete RAG HTTP exchange is bounded by `AGENT_RAG_TIMEOUT_SECONDS`:
default180, finite range1–360 seconds. Connection, headers and streamed body all
share this deadline; the worker's shorter persisted deadline and cancellation
still win. A slow response cannot restart its budget with each chunk.

## Independent review findings corrected

| Finding | Correction and regression |
| --- | --- |
| RAG refusal was discarded by extractive fallback | Preserve typed assessment, remove usable refused citations and retain bounded quarantine IDs |
| Refusal prose could finish a DAG successfully | Explicit outer domain outcome, durable failed step/run and blocked dependent work |
| A normal model refusal was retried as missing citations | Strict JSON refusal branch returns a domain result; invalid JSON remains an execution error |
| Non-calibrated bases could carry misleading score/profile fields | Require null scoring metadata outside calibrated bases |
| Active legacy finished-success checkpoints could bypass the new evaluator | Re-evaluate old active observations under both policies without replaying committed tools |
| Long upstream research requests exceeded a fixed90-second Agent timeout | Operator-bounded complete-exchange deadline, with a real slow-header/stream TCP regression |
| Source text could crowd exact zero/aggregate provenance out of the prompt | Atomic exact-observation packing before optional excerpts; fail explicitly if it cannot fit |
| Source summaries could change admission identity during packing | Preserve validated source admissions and original provenance independently from excerpt length |
| Arbitrary research trace could enter durable event metadata | Extract only bounded known engine/version/count fields; no raw trace copying |
| New tests could appear to prove model quality using controlled responses | Keep actual pretrained admission/upstream inference distinct from explicitly local synthesis/extraction fixtures |

Cross-review also found two companion federation defects: malformed citation
provenance and non-boolean legacy support flags. Those were corrected in RAGs
with rejection tests before publication. No unmeasured source/provider behavior
is counted as verified by this Agent's protocol checks.

## Reproduction

Install both repositories and the Agent development/MCP extras, then run:

```bash
AGENT_ENV=test RAGS_ENV=test python -m pytest -q
ruff check agentic tests --select E4,E7,E9,F
python tests/e2e_v02.py
python tests/e2e_v03.py
```

`tests/e2e_v03.py` accepts a disposable `AGENT_E2E_POSTGRES_URL` for the same
real API/worker flow against PostgreSQL. Its explicit external-RAG mode is used
by the companion research harness and does not mutate that harness's source
corpus. The actual admission harness accepts a separate pinned scorer interpreter,
profile and model cache; it uses frozen calibration fixtures to exercise the
integration contract, not to retune or score held-out quality.

The [recorded actual Qwen HTTP run](https://github.com/MasterLKH180cm/RAGs/blob/feat/spec-driven-v02/reports/agent_admission_http_v03.json)
passed both cases: accepted evidence completed with one local provider HTTP
synthesis call and one usage record; refused evidence persisted a failed run
with zero synthesis calls and zero reservations. This is execution evidence,
not an additional quality benchmark. The companion frozen quality evaluation
failed:9/16 held-out no-answer cases were refused and admitted hybrid Recall@3
fell from0.85 to0.70. No admission profile is enabled by default.

The final local suite passed109 tests with3 external-service skips. Separate
actual HTTP/worker/MCP, upstream and admission runs exercise those service
boundaries; PostgreSQL, Redis, S3 and Compose remain explicit CI gates.

## Limits that remain explicit

- RAG quality gates belong to the frozen RAG evaluation. An approved profile ID
  proves operator selection; it does not make a failing quality candidate pass.
- Citation, numeric and lexical answer/source checks remain heuristic. They do
  not prove semantic entailment of every generated claim.
- Agent gateway reservations cover Agent planning/synthesis. RAG admission,
  embeddings and upstream generation have a separate resource/cost boundary.
- The twelve layers retain the documented boundaries: no arbitrary-code sandbox,
  autoscaler, external task queue, deterministic history replay, vector memory,
  managed secret vault or tamper-evident audit chain.
- Actual disposable external-service tests establish integration correctness,
  not production throughput, failover or live identity-provider administration.
