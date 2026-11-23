# Qwen generation through the existing gateway

## Scope and acceptance

Add an explicit Qwen3-1.7B Q8_0 deployment alongside the existing Inkling
recipe. The historical Qwen admission profile remains immutable and separately
evaluated. Selecting Qwen must never relabel an Inkling deployment, widen a
previously provisioned key, change embedding identity, or imply model quality
from fixture responses.

The bootstrap accepts only the named `inkling` and `qwen` presets. Each creates
one generation alias, one fixed upstream model identifier, two distinct service
keys restricted to that alias, and the existing four inference/listing routes.
The selected alias reaches the key initializer through the private administrative
environment. Existing Inkling calls and generated files retain their defaults.
Receipts bind the selected alias; changing an existing key's deployment fails
closed and requires an explicitly new bootstrap directory and keys.

The Qwen upstream serves `qwen/Qwen3-1.7B`; LiteLLM routes
`gateway/qwen3-1.7b-q8_0-v1` to `openai/qwen/Qwen3-1.7B`. The existing TLS,
PostgreSQL, no-cache, zero-retry, no-fallback, secret separation and prompt-log
policies apply unchanged. A final Compose overlay supplies Qwen application
aliases without changing standalone Inkling defaults. Embeddings remain local.

## Real model evidence

Run verified official Qwen GGUF bytes with a pinned llama.cpp runtime, one CPU
server slot, bounded context and threads, thinking and prompt caching disabled,
and explicit deterministic sampling. Record artifact hashes, runtime identity,
server flags, memory, sample counts, timing and failures. Use the same running
upstream for direct and TLS gateway comparisons. Report queueing at client
concurrency greater than one as queueing, not parallel decoder performance.

Acceptance must exercise production RAG generation and independent Agentic HTTP
API/worker processes, planning, execution, synthesis, evidence references and
usage accounting with actual model inference. Synthetic contract prompts are
permitted, but the upstream must not be a response fixture. Benchmark throughput
and latency retain failures and distinguish validated responses from mere HTTP
success. Frozen admission failures remain visible and cannot be fixed by
retuning its threshold on already-inspected heldout data.

Use three separately created, disposable PostgreSQL databases for the gateway,
RAG and Agentic; install pgvector only in the fresh RAG test database. Stop all
API/worker/model/gateway processes before dropping their databases. Record the
database versions, source hashes, model process memory and actual server
properties. The named administrative test URL authorizes creation of these
randomly named test databases; existing application databases are never reused.

## Reproduce the live acceptance

Install the sibling RAGs checkout and the Agentic `postgres` extra in the calling
environment. Prepare the isolated, hash-locked LiteLLM runtime as described in
`LITELLM.md`, and the verified Qwen artifact/runtime as described by the RAGs
`SELF_HOSTED_QWEN_SPEC.md`. Set `AGENT_TEST_POSTGRES_URL` to an explicitly
disposable PostgreSQL administrator endpoint with pgvector installed.

```bash
python scripts/check_qwen_live.py \
  --model-cache /absolute/path/to/qwen-cache \
  --server /absolute/path/to/llama-server \
  --gateway-python /absolute/path/to/isolated-litellm/bin/python \
  --rags-python /absolute/path/to/RAGs/.venv/bin/python \
  --rags-path /absolute/path/to/RAGs \
  --threads 8 --repetitions 5 \
  --output reports/testing/qwen-live-new-run
```

The report directory must not exist. `--smoke-only` runs the actual model and
full system contracts without the 192-request direct/proxy benchmark. An
optional `--baseline-planner` points to a trusted, previously reviewed planner
source file for a same-model before/after replay; it is never downloaded by the
harness. Each benchmark path has six warmup requests and 30 measured requests
at each of client concurrency 1, 2 and 4. Warmup is excluded from phase latency.

The live acceptance helper validates completed synthesis results, not substrings
found elsewhere in a workflow. Retrieval workflows require both reported planning
and synthesis usage, a synthesis step dependent on retrieved evidence, the exact
expected answer value and matching citation IDs/text from `sample-archive`.
Arithmetic requires the verified value 437 and a completed dependent synthesis.
For agent loops, inspect the completed `agent.run` checkpoint's plan and observed
tool results, require finished/accepted state and agreement with the final result,
and validate the last synthesis in actual observation order. Every usage record
must match the workflow tenant/run and configured gateway route, contain positive
integer input/output counters, and settle to their exact sum; aggregate charges
must match. Retrieval-only, planning-only, stale-checkpoint, wrong-answer and
forged-citation cases must fail. Return only fixed public result metadata and
counter summaries, with fixed error codes that never echo input content.

The dedicated `qwen_live_worker.py` launcher is used only by this isolated
synthetic acceptance. It preserves bounded generated text, finish/refusal fields,
allowlisted token counters, prompt/schema hashes and short exception locations,
so a rejected real response can be diagnosed without changing application
validation. It requires the test environment and the dedicated synthetic tenant;
never use this launcher with user data. No HTTP headers, credentials, request
text, provider metadata or exception messages enter the capture. At most 128
records are retained, with each response text field capped at 16 KiB. These
workflow timings include diagnostic writes. Production workers use their usual
launcher without text capture. The report distinguishes attempted calls from
confirmed inference, including responses rejected by validation.
Capture failures never replace a model response or tool exception. They mark
diagnostics incomplete; the application and benchmark acceptance results retain
their original meaning. The harness stops the worker before collecting its trace.
