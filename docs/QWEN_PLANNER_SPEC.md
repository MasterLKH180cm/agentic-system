# Qwen generation and planner tool-contract acceptance

Specification recorded before implementation, 2026-11-23. This scoped change
repairs the online planning contract and explicitly selects its planning
purpose. Gateway transport capabilities are specified separately; this change
does not approve a model-quality profile.

## Problem and required behavior

The baseline planner supplies allowed tool names without their argument
contracts. Execution validates those arguments against `ARGUMENT_MODELS`, so a
real generation model must guess fields such as `rag.search.question`. The
existing deterministic provider fixtures do not establish that a real model can
produce a valid plan. The prompt also slices serialized JSON at 20,000
characters, which can cut a memory record, string escape or closing delimiter.

1. Supply one complete plan response schema, deriving each tool's argument
   contract from `ARGUMENT_MODELS`. Bind each allowed tool name to its own args
   schema in a step `oneOf`; preserve required fields, bounds, nested definitions
   and the prohibition on additional fields.
2. Advertise only tools allowed by the resolved workflow policy, the current and
   snapshotted feature ceilings, and the loop's read-only restriction. Never
   advertise internal `agent.run` or `workflow.plan` as generated plan steps.
   Fail before calling the model when no executable tool remains.
3. Keep the goal, bound namespace, instructions, evaluator feedback and complete
   tool schemas intact. Pack memory as whole records into a 20,000-byte UTF-8
   JSON message, record how many records were omitted, and preserve input order
   among retained records. Do not slice serialized JSON or silently truncate
   instructions. If mandatory content cannot fit, reject before a provider
   request. ModelGateway's independent atomic token/cost reservations remain
   authoritative; the message cap is not a token-budget allowance.
4. Treat the added schemas as planning guidance. Continue to validate every
   returned step's arguments, generated step count, dependency DAG, allowed
   tools, feature ceilings, memory namespace and approvals before execution.
   Preserve the offline planner, durable execution and accounting contracts.

## Iteration after actual Qwen smoke attempt 1

The preserved `reports/qwen-live-smoke-20261123-attempt1/system.json` records a
real failed Qwen run. The baseline invented `query`/`evidence` arguments, numeric
IDs and dependencies naming tools. The first schema-guided revision corrected
`question` but still emitted numeric IDs, a tool-name dependency, copied the
`top_k: 5` default despite the requested limit, combined hybrid search with row
filters and reused search arguments for synthesis. Neither output is accepted.

Replace the argument-schema table with a single complete response contract:
`steps` contains one to eight objects with string `id`, a permitted `tool`, that
tool's `args`, and `depends_on` containing step IDs. Reuse the execution ID
pattern/bounds; reject unknown root and step fields. Hoist nested definitions to
the response root and keep every `$ref` resolvable there. Remove non-validating
schema annotations such as titles and defaults so they do not suggest values to
copy. Tool-specific branches prevent search fields or fabricated evidence in
`model.summarize.args`. Basic retrieval omits structured-only fields; a separate
structured args branch requires `mode: structured` for filters or aggregation.

The system instruction must distinguish step IDs from tool names, require goal
limits to take precedence over defaults, and state that execution provides tool
results through dependencies. Generate only necessary applicable arguments; do
not invent filters, evidence, citations or answers while planning. Use general
rules, not a fixed `top_k`, goal-specific schema or an output-repair pass.

Expose `plan_response_schema(allowed)` for optional constrained decoding. A
primary planning route explicitly declaring JSON-schema support receives the
schema through `ModelGateway.complete(response_schema=...)`; the gateway must
reject unsupported selected fallback routes instead of dropping the constraint.
Other routes retain schema-guided text generation. Include the complete schema
only once: in the wire response format for opted-in routes, or in the prompt for
other routes. Constrained prompts retain a compact argument-name guide. The
gateway accounts for wire-schema bytes; planner memory packing still preserves
valid whole JSON. Runtime Step/DAG/ControlPlane validation remains authoritative
for cross-step references, uniqueness, permissions, namespace and approval rules
that a static response schema cannot prove.

Every online planning call must explicitly pass `purpose="planning"`, including
the public `plan(goal, model)` form without tenant or workflow identifiers.
Capability selection, provider routing and durable usage must agree on that
purpose; the gateway's synthesis default must never select a different route
for an unbound planning call. Preserve the gateway's default tenant and optional
workflow binding rather than inventing a workflow solely for routing.

## Regression gates

- A captured planning request contains the execution contract's actual fields,
  including `rag.search.question` rather than an invented `query`, and the
  arithmetic tool's required `expression`.
- Policy, feature snapshots/current configuration and loop restrictions filter
  both advertised tool names and schemas. An empty allowed set makes no model
  call. An authorized publication step still requires approval.
- Non-ASCII text, JSON escapes and large memory records leave the emitted
  message valid, bounded JSON. Retained records and mandatory context remain
  complete, and packing does not mutate caller input.
- Invented argument fields, policy-forbidden tools, internal runtime tools,
  invalid dependencies and foreign memory namespaces still fail validation.
- Existing online planning/worker, feature/accounting and offline regressions
  pass. The full two-service smoke remains an integration gate.
- An unbound planning call with separate synthesis-only and planning-only routes
  selects and records the planning route, both with and without its explicit
  JSON-schema capability. A minimal model adapter also receives the planning
  purpose, even when it exposes no route-inspection method.
- Validate the complete schema with the standard JSON Schema validator, including
  nested references, allowed-tool discrimination, string IDs, dependency syntax,
  args isolation, hybrid/structured separation and no copied defaults. Preserve
  the actual failed output as a regression: it must still be rejected without
  repair. Unknown references, duplicate IDs, cycles and valid-looking but
  unauthorized results still fail local runtime validation.

## Real Qwen verification

Use actual `Qwen/Qwen3-1.7B` generation through the production ModelGateway;
record the serving artifact, immutable revision/hash, quantization, runtime and
sampling/thinking configuration. Previous frozen Qwen answerability scoring and
synthetic HTTP provider tests are different evidence and must stay labelled as
such. Do not load a second model concurrently on the constrained CPU host.

Replay the same goal and specification against the baseline planner loaded from
`git show HEAD:agentic/planner.py` and the repaired planner, using the same live
upstream deployment and sampling settings:

```python
goal = "Find the sample retention period, retrieve at most 2 results, then summarize the evidence."
specification = {
    "allowed_tools": ["rag.search", "model.summarize"],
    "allowed_namespaces": ["default"],
    "memory_namespace": "default",
    "max_steps": 8,
}
steps = await planner.plan(goal, gateway, specification=specification, tenant="local")
```

Inspect the actual model response and validation outcome without repairing the
returned JSON. A successful replay must retain a valid retrieval step and a
dependent synthesis step; record any baseline or repaired failures honestly.
Then execute the resulting workflow through the real worker and RAG service,
verify grounded synthesis and usage settlement, and run the same-deployment
direct-versus-LiteLLM comparison. Prompt/schema regressions alone do not prove
model quality, real tool execution, end-to-end success or inference performance.
