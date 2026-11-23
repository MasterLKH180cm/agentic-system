# Explicit structured-output route contract

## Purpose

Real Qwen3-1.7B generation exposed invalid planner JSON despite prompt-supplied
argument schemas. Add an explicit transport capability so a planner can request
constrained JSON generation from a verified compatible deployment. Do not repair
responses, infer capabilities from a model name, or loosen Step/DAG/tool validation.

The existing LiteLLM 1.110.2 OpenAI adapter accepts `response_format`; its pinned
`OpenAIGPTConfig._map_openai_params` passes the supported value through unchanged.
The official API example uses the same nested `json_schema`/`strict` structure:
https://docs.litellm.ai/docs/completion/json_mode . The separate actual local
Qwen/LiteLLM probe is recorded in `reports/qwen-structured-probe-20261123.json`.
Neither the protocol capability nor this feature establishes general model quality.

## Configuration and transport

- Add `ModelRoute.supports_json_schema`, strictly boolean, default `false`.
  `true` is valid only for the `openai` transport. Validate configuration before
  reading credentials. Presets and arbitrary model identifiers never imply support.
- `ModelGateway.complete(..., response_schema=schema)` sends exactly
  `response_format={"type":"json_schema","json_schema":{"name":"agentic_response",
  "strict":true,"schema":schema}}`. No schema means the existing text contract.
- Schema use requires every effective route for that call, including permitted
  fallbacks, to explicitly support this capability. Reject incompatible routing
  before reservation or network access; preserve the existing route order,
  retry/fallback policy and whole-call deadline.
- Preserve the caller's schema without coercion or dropping constraints. Root
  schemas come from application code, never retrieved content or user-supplied
  schema definitions, and must describe an object. Require pure finite JSON, at most 16 KiB of
  serialized UTF-8, 32 levels and 2,048 nodes. References must resolve locally;
  reject remote references, identifier/base changes, dynamic references, cycles,
  and reference graphs expanding beyond 8,192 nodes or 32 levels. Validate the bounded schema with
  the explicitly installed `jsonschema` Draft 2020-12 validator.
- Snapshot the schema before any await. Include the serialized response-format
  wrapper in conservative UTF-8 input reservation and `input_capacity` accounting.
  Enforce a 1,000,000-byte complete JSON request-body limit before each route's
  reservation or network call. Existing text budgets remain unchanged without a
  response schema; provider-reported usage continues to settle reservations.
- For schema requests, parse the returned complete text as unambiguous finite
  JSON and validate against the requested schema. Reject duplicate keys, invalid
  JSON, mismatched shape, truncation and refusal. Return the original text only
  after validation; never strip fences, extract a fragment or invent fields.
  Keep fixed public errors that do not echo schemas, responses or credentials.

## Acceptance

Use existing local HTTP fixtures and durable accounting to prove opt-in schema
forwarding, default request compatibility, budget denial including schema cost,
valid output, invalid output and preserved reported usage. Negative checks cover
invalid route flags/providers, pre-credential validation, oversized/deep schemas,
remote/unresolved/cyclic references, malformed schema definitions, and unsupported
fallback routes with zero requests/reservations. Root acceptance runs the same
planner contracts through the actual Qwen server and TLS LiteLLM gateway.

## Synthesis integration after actual Qwen smoke attempt 2

The preserved `reports/qwen-live-smoke-20261123-attempt2/system.json` records
successful schema-constrained planning and grounded RAG generation, followed by
a synthesis validation failure after the arithmetic tool returned its correct
result. For online synthesis, use the complete
`SynthesisEnvelope.model_json_schema()` contract only when the primary effective
`synthesis` route explicitly supports JSON schema. Select routes against the
workflow feature snapshot and retain the gateway's fail-closed fallback checks.
Restrict `cited_ids` to distinct, admitted source identifiers; with no sources,
require an empty list. Include this schema in `input_capacity` before packing
evidence. After packing, narrow the schema to the source IDs actually present in
the prompt, which can only reduce its serialized cost, and pass it to `complete`
with the same tenant/run and synthesis purpose. Never expose quarantined or
unpacked source IDs as permitted citations. Preserve the offline extractive path,
unopted-in text requests, early evidence/context refusals and all existing local
envelope consistency, citation, numeric-grounding and refusal validation. Tests
must prove wire-schema forwarding and accounting, arithmetic without invented
citations, authorized-source narrowing, low-budget no-request behavior, and
continued rejection of schema-valid but ungrounded or contradictory answers.

## Inline citations after actual Qwen capture 4

`reports/qwen-live-smoke-20261123-attempt4/system.json` preserves a successful
retrieval followed by a well-formed synthesis envelope whose `cited_ids` names
the authorized chunk but whose answer contains no inline citation. Existing
local citation equality correctly rejects that response. Express this existing
contract in two complete object branches under root `oneOf`. The success branch
fixes `outcome: success` and `refusal_reason: null`; when packed sources exist,
its answer must contain at least one `[exact authorized ID]`, and every bracketed
reference must name an authorized ID. Its distinct `cited_ids` must contain at
least one authorized ID. With no packed sources, success has no bracketed
references and an empty citation list. The refusal branch fixes
`outcome: insufficient_evidence`, `refusal_reason: model_rejected` and an empty
citation list, retaining the original bounded answer string without requiring
source citations. Do not force successful answers or suppress legitimate refusal.

Build literal reference patterns only from normalized source IDs. Escape regex
metacharacters, keeping hyphens literal outside character classes, and use an
equivalent single-character run quantifier only when its JSON-serialized UTF-8
representation is strictly shorter than repeated literals. Put the quantified
literal in its own group: the pinned llama.cpp pattern converter can otherwise
merge an escaped atom with preceding literal text before applying repetition.
Do not increase the
16 KiB schema limit, omit sources to fit this representation, relax citations,
or repair generated answers. Test twenty distinct 200-character valid IDs using
long dot/hyphen/colon runs, short dot runs and alternating escaped/literal
characters; require valid bounded schemas, exact literal matching, rejection of
nearby IDs, and nonincreasing schema cost as sources are removed. Retain local
set-equality, provenance, numeric-grounding and envelope validation: static
schemas cannot prove that the two output fields cite exactly the same subset.

## Current synthesis wire contract: synthesis-text-v1

The pattern-based approach above is superseded. Actual smoke 5 and
`reports/qwen-synthesis-probe-20261123.json` recorded HTTP 500 failures for both
the complete and success-only pattern schemas; the original non-pattern
envelope completed. The pinned serving converter applies a string pattern to
serialized grammar characters rather than intersecting it with normal JSON
string escaping, and takes that path before string length bounds. Do not work
around this by banning quotes, backslashes, newlines or other legitimate text.

For explicitly opted-in synthesis routes, `synthesis-text-v1` returns exactly
`outcome`, `text`, `cited_ids` and `refusal_reason`. `text` is unrendered prose;
the model explicitly chooses citations through distinct authorized `cited_ids`.
Use complete success/refusal object branches, ordinary bounded JSON strings and
source-ID enums, with no regex patterns. Success fixes a null refusal reason and
requires at least one selected ID if packed sources exist; observations-only
success has no IDs. Refusal fixes `model_rejected` and an empty ID list.

Strictly validate this new wire shape without accepting the legacy `answer`
field as an alias. Then ALWAYS render the original `text` followed by one
` [ID]` for each model-selected ID, in the model's order. Never condition this
rendering on whether inline citations appear missing, select a source on the
model's behalf, strip or rewrite text, repair invalid JSON, or admit unknown IDs.
The text limit is 7,940 characters: twenty distinct IDs of at most 200 characters
plus their three-character rendering delimiters reserve 4,060 characters, so the
rendered answer stays within the existing 12,000-character envelope limit. This
fixed bound also preserves schema-cost monotonicity when packed sources shrink.

Validate the rendered value through the original `SynthesisEnvelope`, then retain
all citation set-equality, provenance, numeric-grounding and refusal handling.
Inline-looking text is preserved; an unknown or unselected marker still fails
the existing checks. The external answer/citation contract, legacy text-model
requests and offline extractive behavior do not change. Use the new instruction
consistently for capacity accounting and generation; it describes this renderer
and asks for prose plus selected IDs, not for inline formatting. Keep the old
failed reports as evidence, and remove the unused pattern/RLE implementation.

Acceptance covers quoted, escaped and multiline prose; arithmetic and refusal;
single/multiple selected sources; exact 12,000-character rendering; missing,
empty, duplicate or foreign IDs; legacy/ambiguous wire fields; preserved inline
markers; numeric claims supported only by an unselected source; and unchanged
zero-request budget denial and reported-usage settlement on rejected output.
