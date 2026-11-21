# Agentic System v0.3 specification

Status: proposed implementation contract; commit this specification and the
companion specification before changing feature code.

Baseline: reviewed v0.2 at `19eddf60f3c4e4c8a767139226f86b50f89f4f33`.
Companion: `RAGs/docs/SPEC_V03.md`.

## 1. Goal and scope

Preserve a RAG refusal from the authenticated knowledge service through retrieval,
evidence packing, synthesis, loop evaluation, durable results, REST, SSE and MCP.
An answer task without usable evidence must not complete successfully merely
because it returned a refusal sentence. A query that ran successfully but found no
usable evidence remains distinguishable from a failed HTTP or model request.

This iteration addresses concrete correctness in the original Runtime, Tool and
Integration, Knowledge and Memory, Governance, and Observability and Quality
layers. Keep the v0.2 tenant and namespace boundaries, approval separation, lease
fencing, bounded retries, cancellation, model reservations and optional storage
contracts. General code sandboxing, autoscaling and new queue infrastructure are
outside this change. Passing a relevance gate or validating citation IDs is not
proof that every answer claim is entailed by its sources.

## 2. Shared RAG query contract

`rag.search` continues to use the operator-configured RAG URL and the workflow
tenant's RAG credential. It sends `generate=false`; Agent synthesis remains the
separate, budgeted answer-generation operation. Both RAG generation settings must
return the following typed assessment, including for an empty authorized corpus.

| Field | Contract |
| --- | --- |
| `outcome` | `success` or `insufficient_evidence` |
| `supported` | `boolean` or `null`; `false` for refusal, `true` only for explicit model evidence review or a valid exact structured operation, otherwise `null` |
| `refusal_reason` | `null`, `no_candidates`, `below_admission_threshold`, `model_rejected`, or `context_budget_exhausted` |
| `admission.policy_version` | Exactly `evidence-admission-v1` for this contract |
| `admission.decision` | `accept`, `abstain`, or `unassessed` |
| `admission.basis` | `no_candidates`, `ranked_unassessed`, `calibrated_reranker`, `model_review`, `structured_exact`, or `context_budget` |
| `admission.profile_id` | Operator profile identifier or `null`; nonempty identifiers are bounded to 100 characters |
| `admission.score`, `admission.threshold` | Finite JSON numbers or `null`; their meaning belongs to the identified scoring profile |
| `admission.candidate_count`, `admission.accepted_count` | Nonnegative bounded integers; accepted count cannot exceed candidate count |
| `citations` | Existing bounded, authorized source records; always empty for a refusal |

The query-level calibrated gate compares the maximum source score with its
profile threshold. On acceptance RAG preserves the retrieval ordering and the
original authorized candidates, including candidates needed together for a
multi-hop answer. It does not silently rerank or discard individual candidates
using this threshold. `accepted_count` counts retained candidates, not the number
of individual passages exceeding the threshold. Candidate counts and the returned
evidence set must be mutually consistent.

`calibrated_reranker` means an actual relevance assessment. It leaves
`supported=null` unless a separate explicit evidence review supports the answer.
When both checks ran, retain the calibrated admission metadata and represent the
separate review through `supported`. A model review cannot override a failed
calibrated gate. An explicit refusal returns `supported=false`, an `abstain`
decision, a non-null refusal reason, zero accepted candidates and no usable
citations. With `generate=false`, its upstream `answer` is `null`.

Typed contradictions are protocol errors, never successful evidence: examples
include refusal with usable citations, success with `supported=false`, a refusal
with no reason, non-finite scores, unknown decisions or policy versions, and a
calibrated acceptance below its reported threshold. Unknown top-level response
fields such as free-form trace are not copied into trusted assessment metadata.

Existing response streaming limits and source ID validation still apply. Missing
or malformed payloads, HTTP authentication failures, unavailable adapters and
transport errors remain execution failures; do not convert them into knowledge
refusals or silently substitute a different strategy.

## 3. Operator admission policy

RAG owns scorer configuration and calibration:

- `RAGS_ADMISSION_POLICY=compatibility|calibrated_required`.
- `RAGS_ADMISSION_PROFILE` names an operator-controlled JSON file containing the
  profile ID, model and artifact hashes, calibration digest, threshold, windowing
  and aggregation settings.
- The configured scorer is the actual FastEmbed
  `BAAI/bge-reranker-v2-m3-int8` implementation. A lexical overlap heuristic, model
  stub or guessed threshold is not an equivalent verified profile.

Agent enforces the corresponding execution boundary:

- `AGENT_RAG_ADMISSION_POLICY=compatibility|calibrated_required`, defaulting to
  `compatibility` for existing deployments.
- `AGENT_RAG_ADMISSION_PROFILE_IDS_JSON` is a JSON array of 1..32 distinct,
  nonempty profile IDs when calibrated admission is required. Validate the
  configuration at startup; an empty allowlist or unknown policy fails startup.
- Snapshot the resolved policy and allowlist in each new workflow specification.
  Changes to environment configuration do not loosen an active run's policy.
- Do not add caller- or planner-selected URLs, models, thresholds, calibration
  profiles or admission-policy overrides to `SearchArgs`.

In `calibrated_required`, successful passage evidence must have an `accept`
decision, `calibrated_reranker` basis and an allowlisted profile, with valid finite
score/threshold metadata. `supported=true` alone is insufficient. A result whose
only basis is `model_review` does not prove the calibrated gate ran. A valid
`structured_exact` operation is a separate exact-evidence path described below.
Unassessed or unapproved evidence in this mode fails the execution policy; it is
not relabelled as an ordinary no-candidates refusal.

In `compatibility`, a legacy response without the new assessment can be normalized
to explicitly `unassessed` evidence, never `supported=true`. An empty legacy
result becomes `insufficient_evidence/no_candidates`. Legacy `supported=false`
must quarantine all associated candidates. A malformed typed v0.3 result cannot
use the legacy fallback to evade validation. A supplied explicit refusal always
takes precedence over compatibility handling.

## 4. Retrieval observations and evidence quarantine

`rag.search` returns a successful tool execution with its typed query assessment
under a `retrieval` object. Keep that domain outcome distinct from the terminal
tool `outcome`: a successful retrieval with no hits must not fail the entire DAG
before another independent retrieval branch can contribute useful evidence.
Expose requested and actual strategy as bounded identifiers; preserve the actual
strategy used without pretending an unavailable upstream engine executed.

A refused retrieval contains no usable citations and no reconstructed extractive
answer. Retain only its validated assessment and diagnostic counts, not rejected
source bodies. `pack_evidence` must apply the same rule recursively to nested
`data.merge.outputs` and to resumed observations. A downstream tool, model,
memory result or lexical evaluator cannot restore a quarantined source merely by
repeating its citation ID. Independent authorized evidence in another branch
remains usable; one refused branch does not poison the whole merged result.

Memory remains a separate tenant/namespace-authorized source. It cannot claim
calibrated RAG admission or rewrite a refused observation's provenance. Existing
checkpointed source and memory identities remain bounded and validated before
they enter a synthesis prompt. Refusal metadata is data, not a grant of tool or
namespace permissions.

### Exact zero values and structured provenance

Test for the presence and type of a result, not its truthiness. A finite
`math.calculate` value of zero is useful evidence. A structured count or sum of
zero can also be valid when an authorized dataset was actually evaluated; it is
different from having no authorized dataset at all.

RAG structured evidence carries
`structured_result.provenance.datasets=[{document_id,revision,profile_id}]`.
Require 1..20 distinct validated dataset identities for a usable exact result;
each identity must come from the authenticated RAG service's authorized corpus.
More than 20 contributing datasets must fail explicitly rather than truncating
provenance while presenting a complete aggregate. No dataset means refusal, even
if an aggregation helper would otherwise return numeric zero.

Validate finite aggregate values, operation, field where applicable, and
nonnegative matched-row count. Preserve the bounded aggregate and provenance as
typed observations rather than inventing passage citations or copying entire
row collections into prompts. An exact result has `structured_exact` admission
and may set `supported=true`. A valid zero result is not rejected because the
passage-citation array is empty. `rag.search` preserves this bounded structure;
any added structured query arguments must reuse the companion API's finite
predicate/aggregation schemas and tenant filtering, with no generic SQL.

## 5. Typed synthesis and complete budget accounting

For model-backed synthesis, request and validate one JSON object:

```json
{
  "outcome": "success",
  "answer": "Session timeout is 30 minutes. [c1]",
  "cited_ids": ["c1"],
  "refusal_reason": null
}
```

The object has only these declared fields. `outcome` uses the same two values as
the query contract. The answer is at most 12,000 characters; cited IDs are unique,
match the existing source-ID syntax, number at most 20 and must exactly match the
IDs cited in the answer. A success has a nonempty answer and no refusal reason.
It must use admitted source evidence or a valid typed observation; an arithmetic
or exact structured answer does not need a fabricated passage citation.

A model-declared refusal uses `insufficient_evidence`, no cited IDs and
`refusal_reason=model_rejected`. Normalize its user-facing text to a fixed refusal
message and return a typed result. It is not a missing-citation exception and
does not trigger a generic tool retry. Invalid JSON, unknown fields, contradictory
outcomes and fabricated citations remain protocol failures. Do not classify
normal prose by searching for an English refusal phrase.

Before making a model request, eliminate quarantined inputs and pack complete
JSON objects. With no usable source or observation, return a typed refusal
without invoking a model or making a new usage reservation. Preserve a validated
upstream refusal reason when it explains the result; use `no_candidates` when no
usable evidence was supplied, and `context_budget_exhausted` when available
evidence cannot fit the permitted prompt.

The existing context cap is a maximum, not the total model budget. Account for
the system instruction, serialized schema/format instructions, question, source
headers, observations, JSON escaping, message framing and reserved output
envelope before selecting context. Use the ModelGateway's conservative accounting
units consistently. The full JSON answer envelope, including field names and
citation IDs, must fit the configured output limit. Do not slice serialized
prompts or model JSON into invalid fragments to satisfy a cap. Concurrent budget
reservations remain atomic; exhausted execution budgets retain the existing
`budget_exhausted` result instead of becoming an evidence refusal.

Offline synthesis uses the same normalized result contract and evidence filter.
It may present admitted extractive evidence or verified exact observations; it
does not manufacture model verification or claim that an unassessed legacy
retrieval passed semantic admission.

## 6. Durable DAG and loop behavior

A final synthesis refusal returns outer `outcome=insufficient_evidence`, a fixed
`stop_reason`, a validated `refusal_reason`, fixed answer text and empty citations.
Use `no_usable_evidence`, `context_budget_exhausted` or `model_refused` for the
synthesis stopping reasons. The existing `Store.complete` path persists the
result and marks that step and its workflow failed. Dependent publication is not
claimed or approved after this failure. The search observation itself may have
completed with a nested retrieval refusal, as described above.

Within `agent.run`, a synthesis or retrieval refusal creates an explicit negative
assessment and bounded refinement, not a successful answer or a generic tool
exception. The evaluator must respect quarantined evidence before selecting an
answer. Valid calibrated relevance admission may bypass only the existing
question-to-source lexical-overlap veto; it must not bypass unknown-citation,
unsupported-numeric-claim or answer-to-source support checks. This preserves
semantic paraphrases without equating relevance with answer entailment.

Persist assessments, normalized observations and the next cursor before advancing
the loop. Recovery uses the committed typed assessment and does not repeat an
already committed retrieval merely to reconstruct its refusal metadata. At the
round cap, preserve `outcome=insufficient_evidence`,
`stop_reason=rounds_exhausted`, empty citations and `last_refusal_reason` from the
fixed enum. Preserve an already completed good result when a different branch
has no evidence. Existing cancellation, deadline, budget and lease fencing take
precedence over late success or refusal writes.

### Events and public inspection

Add bounded assessment metadata to existing completion and runtime events:
outcome, refusal reason, admission decision/basis, approved profile ID, strategy
ID, round number and candidate/accepted/usable counts. Event types and stopping
reasons are fixed code-defined values. Do not copy model explanations, upstream
trace, queries, source text, full row data, prompts, credentials or arbitrary
metadata into events or telemetry. Score diagnostics must be finite and match the
declared profile; do not present incomparable scores as universal confidence.

REST workflow inspection, authenticated SSE and MCP status/events expose the
same durable outcome and safe metadata. MCP must parse the actual returned result
and inspect the terminal workflow; a nonempty tool response is not evidence of a
successful answer task.

## 7. Compatibility and storage

No database migration or new run/step status is required. Reuse existing JSON
specification, result, checkpoint and event payload columns. Add an evidence
contract version to newly written snapshots/checkpoints and handle old JSON
explicitly. Historical finished workflows remain readable and unchanged; legacy
active observations without admission evidence are unassessed, never silently
upgraded. A required-policy run cannot resume by treating old unassessed data as
calibrated evidence.

Keep SQLite and PostgreSQL behavior equivalent for result persistence, checkpoint
resume and fencing. Existing approvals, model accounting, tenant and namespace
checks, response caps and request-body limits remain regression requirements.
Do not claim an external engine or profile is verified until its real configured
implementation and artifact have passed the applicable checks.

## 8. Acceptance gates and independent review

| ID | Required evidence |
| --- | --- |
| AG3-T01 | Empty authorized corpus through the real RAG HTTP API, Agent API and independent worker: default answer DAG ends failed with persisted `insufficient_evidence`, empty citations, no synthesis model call and no dependent publish execution. |
| AG3-T02 | Valid refusal metadata survives retrieval and nested merges. Refusal-with-citations and other contradictory typed payloads never yield success. The refusal cannot be revived by extractive fallback, repeated IDs, memory reuse or a lexical evaluator. |
| AG3-T03 | An empty/refused retrieval branch plus an independent admitted branch can produce a successful answer using only the admitted evidence. A finite arithmetic zero and a provenance-backed structured count of zero remain valid; missing, forged or over-limit dataset provenance fails. |
| AG3-T04 | Actual local provider HTTP protocol tests exercise the finite synthesis JSON envelope: normal success, normal refusal without generic retry, invalid JSON, unknown/fabricated citations, contradictory fields and oversized envelope. Distinguish local provider fixtures from live paid-model quality evidence. |
| AG3-T05 | Full prompt/schema/framing/output accounting is tested with non-ASCII text, JSON escaping and a small configured budget. Whole objects remain valid JSON; evidence packing does not exceed the call budget or reserve/call a model when no evidence remains. |
| AG3-T06 | A loop first receives a genuine refusal, refines and succeeds with admitted evidence; another loop exhausts its bound with explicit final refusal. A semantic paraphrase admitted by the real configured profile is not rejected solely for missing question-term overlap. Citation, numeric and answer-support guards still reject invalid answers. |
| AG3-T07 | Crash/resume after a committed refusal observation does not repeat that retrieval. Cancellation, expired leases, exhausted budgets and deadlines fence late results. Repeat the durable-result path on SQLite and PostgreSQL. |
| AG3-T08 | Operator required mode rejects missing configuration, unapproved profiles and unassessed passage evidence. Actual FastEmbed BGE scorer/artifact and frozen calibration/admission fixtures exercise pass and abstain decisions; report held-out evidence separately from configuration checks. Compatibility remains explicitly unassessed. |
| AG3-T09 | Real configured upstream retrieval engine -> RAG HTTP -> Agent worker -> result preserves actual strategy/engine identity and original authorized source IDs. Unsupported or unavailable engines fail explicitly; fixtures or fallback strategies cannot count as executing that engine. |
| AG3-T10 | Both real stdio MCP services initialize and call their HTTP-backed tools. Parse RAG outcome, submit an answer task, poll Agent terminal status and inspect durable events; verify success and refusal agree with REST/SSE. A handshake or nonempty response alone fails this gate. |
| AG3-T11 | Unique query/source/credential sentinels do not appear in new event or telemetry payloads. Unknown free-form upstream reasons and arbitrary metadata cannot enter trusted event fields; tenant and namespace isolation remains enforced. |

Execution order: commit both specifications -> implement -> run focused regression
tests and real HTTP/worker/MCP flows -> independent review -> fix findings ->
publish the exact evidence and remaining limitations. Do not change an acceptance
criterion to disguise an unavailable model, unrun service or failing quality case.
