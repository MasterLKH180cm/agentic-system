# Provider response and approval corrections

This specification precedes implementation of the follow-up review on master
`732c10728c2f8014388af03a951f73fdbb1c95e5`.

## Findings and required behavior

1. The outer provider response currently uses permissive JSON decoding, although
   generated structured output rejects duplicate fields and nonfinite numbers.
   A duplicate `usage` key can replace an already reported charge. Every provider
   envelope, planning result, synthesis result and RAG response must share JSON
   decoding that rejects duplicate keys, nonfinite values (including exponent
   overflow), invalid Unicode and excessive nesting with a controlled ValueError.
   Rejected envelopes retain their conservative reservation and cannot trigger
   fallback. Well-formed usage is settled before a malformed content contract is
   rejected, preserving actual provider charges.
2. A provider response can mix text with tool calls while omitting a terminal
   reason. Text-only completion must reject OpenAI tool/function-call fields and
   Anthropic effectful/unknown content blocks. Anthropic thinking and redacted
   thinking may accompany text but are never exposed as answer content. Response
   collections and each selected text block require their actual expected types.
   Missing finish reasons remain compatible with existing providers; explicitly
   invalid reasons remain errors. Valid text responses preserve their content.
3. API approval and step approval requirements currently coerce strings and
   numbers into booleans. Approval payloads and `approval_required` must accept
   actual booleans only; direct storage approval must repeat this check before
   any transaction. Malformed approvals leave status, events and worker
   eligibility unchanged. Explicit true/false retain their current meanings.
4. Constructing `ModelRoute` directly bypasses price and identity validation
   performed by the environment parser. Both construction paths must enforce
   finite nonnegative prices, known transports, bounded nonempty names/models,
   and valid distinct purposes before budget reservations or credential use.
5. Authenticated model, RAG and MCP transports inherit ambient HTTP/SOCKS proxy
   and CA environment settings, despite sending application credentials. These
   clients must use explicit transport configuration (`trust_env=False`), retain
   ordinary certificate/hostname verification, and continue to honor an explicitly
   configured model-route CA file. A proxy-trap regression must show that a
   credentialed local provider request never reaches an ambient proxy.

## Knowledge-plane selection integration

The Compose RAG service must forward the new model revision, dimensions,
document/query prefix and input-type selectors, authentication mode and custom
token window fields. Operators can create a model selection file using the RAG
CLI and combine it with their existing credentials. Agent API/worker must not
receive these embedding secrets or install model/tokenizer dependencies. Existing
image extras remain unchanged; optional `encoders`, `sentence-transformers` and
`tokenizers` extras are selected explicitly for the RAG image.

## Compatibility and scope

The previous CR-05/08/10 fixes, tenant/namespace isolation, durable lease fencing,
at-least-once execution, purpose routing, fallback policy and conservative UTF-8
budget estimation remain unchanged. Changing RAG embedding models/tokenizers is
an operator action in the separate knowledge plane, never a workflow permission
or a reason to weaken Agentic's model budget. Historical Qwen measurements and
quality gates are not rewritten or promoted.

## Acceptance

- Real loopback HTTP provider fixtures demonstrate rejected duplicate/nonfinite
  JSON, malformed content, tool-call blocks, valid reasoning-plus-text and retained
  accounting without fallback.
- API and direct storage regressions prove malformed approval values cannot make
  a pending effect executable; legitimate approval and rejection still work.
- Configuration regressions cover both direct and environment construction.
- Run complete unit/ordinary integration suites, unchanged coverage gates,
  Ruff, and the existing actual two-service HTTP/worker/MCP smoke with the current
  RAG companion. External resource selections remain explicit.
