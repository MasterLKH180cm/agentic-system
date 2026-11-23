# Qwen integration and measurements — 2026-11-23

## Outcome and evidence boundaries

**Actual Qwen generation now completes the tested RAG and Agentic workflows
through the TLS LiteLLM gateway. The separate raw generation-contract benchmark
failed, so this report makes no speedup claim.** These are different acceptance
results and both remain visible in the [full system report](../reports/qwen-live-performance-20261123/system.json).

| Evidence | Result | Scope |
| --- | --- | --- |
| RAG generation and three Agentic workflows | **Passed** | Real model inference, independent application processes, PostgreSQL/pgvector, completed synthesis, citations and reported usage |
| Direct and gateway raw generation contracts | **Failed** | 192 requests including warmup; 160 passed and 32 failed behavioral validation |
| Direct/gateway performance comparison | **Not permitted** | Descriptive timings are retained; `speedup_computed` is `false` |
| Agentic feature CI | **Passed** | All eight jobs in run `45174330740`; 939 distinct pytest cases including the actual S3 test |
| RAG feature publication | **Merged; master CI passed** | PR #14 and run `45169626126` |
| Fresh native-checkpoint admission replay | **Completed; quality failed** | 21/21 correctness checks, 7/11 quality gates; exit 2, promotion failed; profile remains disabled by default |
| Agentic evidence publication | [PR #11 and current checks](https://github.com/MasterLKH180cm/agentic-system/pull/11/checks) | The PR records the final commit's checks and merge state; the first feature run below identifies its own tested revision |

The earlier Qwen integration was an admission scorer using the native
`Qwen/Qwen3-1.7B` checkpoint. This generation deployment uses the official
Qwen3-1.7B **Q8_0 GGUF** artifact with llama.cpp. Sharing a model family does not
make the runtimes, quantization, prompts, tasks or quality measurements
interchangeable. Inkling remains a separate explicit deployment option. Neither
model is silently substituted for the other, and default Agentic generation
remains offline. See the [gateway specification](QWEN_GATEWAY_SPEC.md) and
[deployment guide](LITELLM.md).

## Measured deployment and source identity

The final run used the same running upstream for the direct benchmark and then
the TLS gateway benchmark. Both paths required authentication. The gateway was
LiteLLM **1.110.2**, with the immutable alias
`gateway/qwen3-1.7b-q8_0-v1`, distinct model-restricted service keys, no proxy
retry/fallback/cache, and an isolated gateway environment. The application
databases were three distinct disposable PostgreSQL **17.12** databases; the
RAG database used pgvector **0.8.7**. These facts and the observed server settings
are recorded in the [system report](../reports/qwen-live-performance-20261123/system.json).

| Deployment property | Recorded value |
| --- | --- |
| Model repository | `Qwen/Qwen3-1.7B-GGUF` |
| Model revision | `90862c4b9d2787eaed51d12237eafdfe7c5f6077` |
| Artifact | `Qwen3-1.7B-Q8_0.gguf`, 1,834,426,016 bytes |
| llama.cpp | `b12631`, revision `e300919cc704ddde3a8a8346a8714f57eeeee71b` |
| Host | x86_64 CPU, nine logical CPUs visible, 10,451,423,232 bytes RAM, no accelerator |
| Server | Eight threads, one parallel slot, 4,096-token context |
| Sampling and cache controls | Temperature 0, seed 42, thinking disabled, prompt cache disabled, cache RAM 0 MiB |
| Benchmark output cap | 512 tokens per request |

The [deployment manifest](../reports/qwen-live-performance-20261123/deployment.json)
retains the full artifact hash. The [performance provenance record](../reports/qwen-evidence-20261123/performance-provenance.json)
binds the four raw files, original derived analysis, analysis helper and 78
measured source files. The measured source revisions were Agentic
`9ca69e2afc61294791c450b9d509fa55ff78be2f` and RAGs
`0f72f4569e26528dc0c480baec7ad6320160d1ce`.

The measured `scripts/check_qwen_live.py` belongs to the first Agentic feature
commit `9ca69e2`; its SHA-256 is
`fd06fa2b5cdbe396a84cbc438d023f9c63514e65dc52ba40a4580f232d6088c2`.
The later RUF021 formatting correction only adds parentheses to an existing
boolean expression. Its SHA-256 is
`a9af73b77d9a3b2a9eccef59e651a03faaa759ba4376eb3f87158c8eeac281a3`.
The two files have identical Python ASTs when source positions are omitted;
they are **not byte-identical**. The recorded measurement continues to identify
the original bytes, rather than claiming the later file was executed.

## Actual application workflows

The final performance run repeated all application acceptance checks before
running the raw benchmark. The following are **one observed elapsed time per
workflow**, not latency distributions. API, worker, database, retrieval,
generation and the bounded synthetic diagnostic writes contribute to these
times. The [acceptance records](../reports/qwen-live-performance-20261123/system.json)
retain the actual completed steps, checkpoint observations and usage.

| Workflow | Verified result | Elapsed seconds | Reported generation calls | Input / output tokens |
| --- | --- | ---: | --- | ---: |
| RAG generation | `14 days`; exact `sample-archive` source and matching citation | 4.432 | One RAG generation call | 121 / 52 |
| Explicit arithmetic | `19 × 23 = 437`; calculation and dependent synthesis completed | 5.532 | One synthesis call | 261 / 36 |
| Planned retrieval | Valid generated plan, retrieval, then cited `14 days` synthesis | 24.006 | One planning + one synthesis | 684 / 194 |
| Adaptive retrieval | Completed loop, accepted checkpoint observation and matching final cited result | 23.115 | One planning + one synthesis | 684 / 194 |

The three Agentic workflows settled exactly **297**, **878** and **878** tokens,
respectively. RAG independently reported 173 generation tokens. There was no
configured pricing schedule, so these records do not establish a monetary cost.
The standalone current-planner replay also passed: it produced string step IDs,
the requested `top_k: 2`, valid tool arguments, and a synthesis dependency on the
retrieval step. This probe is separate from the workflow call totals above.

Acceptance requires completed synthesis, the correct answer value, matching
source IDs and source text, the expected route and purpose, positive integer
provider counters, and exact charge settlement. For the loop it also checks the
finished/accepted state and the actual ordered checkpoint observations. A
retrieval result containing `14`, a plan without execution, or an HTTP 200 alone
cannot pass these checks. See [the acceptance helper](../scripts/qwen_acceptance.py)
and [its specification](QWEN_GATEWAY_SPEC.md).

### What `synthesis-text-v1` changes

An opted-in model now returns exactly `outcome`, `text`, `cited_ids` and
`refusal_reason`. It supplies the prose and explicitly chooses authorized
source IDs. The application **always** renders that unmodified prose followed
by one ` [ID]` for each chosen ID, in the model's order. The model is therefore
not being credited with generating inline citation formatting. The public
`SynthesisEnvelope` remains unchanged.

This is an explicit wire contract, not conditional repair of a missing citation.
The renderer never chooses an ID, repairs JSON, rewrites prose or accepts an
unknown source. The local envelope, citation set equality, provenance, numeric
support and refusal checks still run after rendering. A success with packed
sources requires at least one model-selected ID; observations-only arithmetic
requires none. Refusal remains a valid separate branch. Quotes, backslashes and
newlines remain legal text. The 7,940-character text limit reserves space for
twenty 200-character IDs and their delimiters within the existing
12,000-character final answer limit.

Schema support is explicit on every effective route, including fallbacks. Schema
cost is reserved before evidence packing; only IDs actually packed are offered
to the final request. Rejected output retains settled provider-reported usage.
Legacy and offline paths retain their existing contracts. See the
[structured-output specification](QWEN_STRUCTURED_OUTPUT_SPEC.md).

These checks establish the tested source identity and numeric consistency;
they are not a general semantic-entailment proof. This live generation smoke
used the compatibility admission path. It does not establish that the separate
frozen native admission profile passed its quality gates.

## Failures retained during implementation

The implementation was revised against real rejected outputs. Earlier failures
were not rewritten as successful evidence, and the acceptance checks were not
weakened to accommodate them.

| Run | Observed result | Consequence |
| --- | --- | --- |
| [Attempt 1](../reports/qwen-live-smoke-20261123-attempt1/system.json) | Both baseline and then-current planners failed. Outputs included numeric IDs, wrong argument shapes and invalid dependencies. | Supply the full allowed response/tool schemas, explicit route capability and cross-field instructions; retain Step/DAG validation. |
| [Attempt 2](../reports/qwen-live-smoke-20261123-attempt2/system.json) | Constrained planning and RAG generation passed. The arithmetic tool returned 437, but synthesis was rejected. Raw synthesis text was not captured. | Add explicit synthesis schema support; do not infer an unrecorded exact failure cause. |
| [Attempt 3](../reports/qwen-live-smoke-20261123-attempt3/system.json) | Arithmetic completed; retrieval synthesis was rejected. | Add bounded diagnostics in the isolated synthetic worker to distinguish envelope and citation failures. |
| [Attempt 4](../reports/qwen-live-smoke-20261123-attempt4/system.json) | Captured retrieval synthesis selected the correct `cited_ids` but had no inline reference in `answer`; local set equality rejected it. | Test a schema expressing the inline requirement without adding references after rejection. |
| [Attempt 5](../reports/qwen-live-smoke-20261123-attempt5/system.json) | The pattern-based synthesis request failed with an HTTP error; the first arithmetic workflow did not complete. | Preserve the failure and replace the incompatible pattern representation with the explicit `synthesis-text-v1` contract. |
| [Attempt 6](../reports/qwen-live-smoke-20261123-attempt6/system.json) | RAG and all three Agentic workflows passed using the new wire contract. | Repeat application acceptance in the final performance run, then retain the independent raw benchmark result. |

The [isolated synthesis probe](../reports/qwen-synthesis-probe-20261123.json)
recorded HTTP 500 for both the full pattern schema (44.478 s) and its success-only
branch (34.166 s), with the error “The model produced output that does not match
the expected peg-native format”. The non-pattern envelope returned HTTP 200
(3.726 s), but invented `source_1` in an observations-only case and was not a
quality pass. The [pinned converter source](https://github.com/ggml-org/llama.cpp/blob/e300919cc704ddde3a8a8346a8714f57eeeee71b/common/json-schema-to-grammar.cpp)'s pattern path does not apply normal JSON
string escaping and length handling in the same way as its ordinary string
path. Removing only `oneOf` did not resolve the observed error. The unused
pattern/RLE implementation was removed; legitimate text characters were not
banned to accommodate it.

## Raw generation-contract benchmark

The [direct report](../reports/qwen-live-performance-20261123/direct.json),
[gateway report](../reports/qwen-live-performance-20261123/gateway.json), and
[derived analysis](../reports/qwen-performance-analysis-20261123.json) all retain
the failed gate. Each path ran six warmup requests, then five repetitions of
each of six cases at client concurrency 1, 2 and 4:

```text
2 paths × (6 warmup + 3 phases × 6 cases × 5 repetitions) = 192 requests
```

There were **180 measured requests** and **12 warmup requests**. Every measured
phase attempted all 30 requests: **25 passed and 5 failed**. Each warmup had five
passes and one failure. All 192 responses passed schema validation; the 32
failures were `grounded-answer` cases with `behavior_mismatch`.

| Fixed synthetic case | Passed / attempted, both paths including warmup |
| --- | ---: |
| `grounded-answer` | 0 / 32 |
| `insufficient-evidence` | 32 / 32 |
| `json-extraction` | 32 / 32 |
| `readonly-tool` | 32 / 32 |
| `tool-argument-boundary` | 32 / 32 |
| `untrusted-instruction` | 32 / 32 |

Thus five of the six case types passed throughout this fixed suite; this is
not a general model accuracy estimate. The raw benchmark records do not retain
response bodies, so they do not prove that all 32 rejected responses had
identical text. A **separate single-response diagnostic** retained
`{"answer":"14 days","cited_ids":[],"abstain":true}` for the grounded case:
[diagnostic evidence](../reports/qwen-contract-diagnostic-20261123.json).
That illustrates a grounding/abstention contract failure, not evidence that all
failed requests invented the factual value.

The raw suite checks generation contracts and does not execute the production
retrieval, embeddings, database or Agentic workflow. Its prompts and acceptance
criteria were preserved rather than changed to match the new application
synthesis contract. The application passes therefore coexist with the raw
benchmark failure.

### Descriptive latency, including failed attempts

These percentiles are recomputed from **all 30 attempted measured requests** in
each phase, including the five behavioral failures. Warmup is excluded. The
source reports also contain successful-only percentiles with **n = 25**; those
are a different population and are not used in this table. Percentiles use
linear interpolation at `(n - 1) × q`, as recorded in the derived analysis.

| Path | Client concurrency | Passed / failed | Phase wall seconds | All-attempted p50 seconds | All-attempted p95 seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| Direct | 1 | 25 / 5 | 110.540 | 3.380 | 5.508 |
| Direct | 2 | 25 / 5 | 96.706 | 6.070 | 9.769 |
| Direct | 4 | 25 / 5 | 113.273 | 13.568 | 18.383 |
| TLS LiteLLM | 1 | 25 / 5 | 100.728 | 3.047 | 5.135 |
| TLS LiteLLM | 2 | 25 / 5 | 129.751 | 6.568 | 15.379 |
| TLS LiteLLM | 4 | 25 / 5 | 108.559 | 14.050 | 16.833 |

### Descriptive throughput, with separate denominators

Attempted requests/s is `30 / phase wall seconds`; validated requests/s is
`25 / phase wall seconds`. Each phase reported 5,005 prompt tokens and 705
completion tokens across all attempts; 595 completion tokens belonged to
validated responses. The completion rates below divide those counts by the
whole phase wall time. They are **not decoder-only token rates**.

| Path | Client concurrency | Attempted requests/s | Validated requests/s | All completion tokens / wall second | Validated completion tokens / wall second |
| --- | ---: | ---: | ---: | ---: | ---: |
| Direct | 1 | 0.2714 | 0.2262 | 6.378 | 5.383 |
| Direct | 2 | 0.3102 | 0.2585 | 7.290 | 6.153 |
| Direct | 4 | 0.2648 | 0.2207 | 6.224 | 5.253 |
| TLS LiteLLM | 1 | 0.2978 | 0.2482 | 6.999 | 5.907 |
| TLS LiteLLM | 2 | 0.2312 | 0.1927 | 5.433 | 4.586 |
| TLS LiteLLM | 4 | 0.2763 | 0.2303 | 6.494 | 5.481 |

All 192 requests explicitly reported zero cached prompt tokens. Reasoning-token
counters were not reported, so they must not be represented as measured zeros.
Disabled prompt caching and reported zero cached tokens do not establish the
absence of every hardware/runtime cache.

Direct ran before gateway, with no interleaved/randomized path ordering. The
single server slot means concurrency 2 and 4 include queueing and do not
demonstrate parallel decoding. Non-streaming elapsed times include prompt
processing, generation, transport and validation; there is no TTFT measurement.
With only 30 attempted samples per phase there is no confidence interval or
significance claim. The behavioral gate failed, and the analysis explicitly
sets `comparison_permitted: false` and `speedup_computed: false`. Neither apparent
differences between table rows nor matched deployment identity establish a
LiteLLM performance gain or production throughput.

### Observed memory

After both paths, the model process reported peak/current RSS of
2,442,108,928 bytes; the gateway reported peak RSS of 636,010,496 bytes and current
RSS of 635,076,608 bytes. `VmHWM` is a lifetime process high-water mark and `VmRSS`
is a point observation. They are not per-path or per-concurrency measurements.
The separate child peak of 2,441,547,776 bytes is not aggregate system memory.
See the [analysis memory scope](../reports/qwen-performance-analysis-20261123.json).

## Software verification and publication

The [Agentic local verification manifest](testing-results-qwen-20261123.json)
records **938 distinct executed pytest cases**, with zero failures, errors or
selected skips: 506 unit, 413 ordinary integration, and 19 actual
PostgreSQL/Redis cases. The single actual S3 case was explicitly not executed
locally. Coverage across all 24 production Python modules was **90.74% lines**
and **84.69% branches**, above the unchanged 85%/75% floors.

[Agentic PR #11 CI 45174330740](https://github.com/MasterLKH180cm/agentic-system/actions/runs/45174330740)
passed **all eight jobs**. Its selected pytest suites passed **506 unit + 413
ordinary integration + 20 external = 939 cases**, including actual S3. Separate
jobs also exercised Chromium, Compose, the pinned LiteLLM process/container,
RAG/worker/MCP flows and research adapters. Those process checks are not added
to the distinct pytest count, and green software CI does not change the failed
model-quality gate.

The [RAG local verification manifest](https://github.com/MasterLKH180cm/RAGs/blob/0f72f4569e26528dc0c480baec7ad6320160d1ce/docs/testing-results-qwen-20261123.json)
records **987 unit + 439 ordinary integration + 39 actual PostgreSQL/pgvector =
1,465 cases**, with zero failures, errors or selected skips. Coverage across all
49 production Python modules was **85.42% lines** and **78.20% branches**, above
the unchanged 80%/72% floors.
[RAG PR #14](https://github.com/MasterLKH180cm/RAGs/pull/14) merged feature commit
`0f72f4569e26528dc0c480baec7ad6320160d1ce` as
`f7b76d847cefca23a1142f66bb201d1dc01931ee`.
[Master CI 45169626126](https://github.com/MasterLKH180cm/RAGs/actions/runs/45169626126)
passed its seven automatic jobs; the manual full frozen admission job was
skipped. That skipped manual job is not fresh native admission evidence.

The evidence/documentation update is tracked in
[Agentic PR #11](https://github.com/MasterLKH180cm/agentic-system/pull/11), with
[checks for its current revision](https://github.com/MasterLKH180cm/agentic-system/pull/11/checks).
The recorded first-feature run above verifies `9ca69e2`; it must not be
relabeled as a later run. The subsequent update retains measured production
source bytes and adds the reports, standalone analysis utilities, documentation
and the explicitly recorded AST-equivalent harness formatting clarification.

## Fresh native admission replay — completed, quality failed

The [fresh verification report](../reports/qwen-admission-replay-20261123.json)
was written at **2026-11-23 13:07:37.529975 UTC**, after an actual offline-cache
native Qwen replay. All **21/21 correctness checks passed**, but only **7/11
quality gates passed**. With quality enforcement enabled, both the report and
process returned **exit 2**, and `promotion_status` is **`failed`**. The frozen
admission profile therefore **remains disabled by default**. These are fresh
results, separate from the 192 Q8 generation requests and the historical report.

The model was `Qwen/Qwen3-1.7B`, revision
`70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`, using CPU bfloat16 forwards,
PyTorch `2.15.0+cpu`, Transformers `4.57.6`, eager attention and two
intra-op/inter-op threads. The immutable profile ID is
`0be2ce8f8c7eee905c58a231576e7f5ddc5c7e94b561cff603f3398dcd5ac8e4`;
the profile file SHA-256 is
`b8ed84a9d97f23ab9e49a0b40e8fbbec6b91224e642ef97efb763f71605abf32`.
The threshold remained **10.75**, with the existing holdout and regression
datasets unchanged. Full identities, input hashes and derived metrics are in
the [admission analysis](../reports/qwen-admission-analysis-20261123.json).

| Native replay measurement | Recorded value |
| --- | --- |
| Wrapper start / finish, UTC | `12:35:59.141077` / `13:07:40.086474` on 2026-11-23 |
| Wrapper wall time | **1,900.945504 s**, or **31 min 40.946 s** |
| Verifier's recorded elapsed time | **1,897.829 s** |
| Reconstructed forwards | **472**, from complete metadata and the reviewed scorer |
| Child peak RSS | **3,993,993,216 bytes**, or **3.719696 GiB** |
| Wrapper deadline | 3,600 s; complete report written before the deadline |

The [resource record](../reports/qwen-admission-replay-20261123-resources.json)
binds the report, wrapper and progress-log hashes, and records an execution-time
snapshot of the three verification/evaluation source files, frozen profile and
two datasets. The [original wrapper](../reports/qwen-evidence-20261123/replay_operator.py)
and [progress log](../reports/qwen-evidence-20261123/native-replay-progress.log)
are preserved and bound by the
[native provenance record](../reports/qwen-evidence-20261123/native-provenance.json).
The analysis also records a broader source snapshot taken at analysis time;
that later snapshot does not retroactively attest execution.
No other model inference was scheduled during this replay.

The same four quality gates failed as in the
[2026-11-21 historical report](https://github.com/MasterLKH180cm/RAGs/blob/f7b76d847cefca23a1142f66bb201d1dc01931ee/reports/admission-verification-qwen-v03.json):

| Failed quality gate | Fresh result | Required |
| --- | --- | --- |
| Held-out unanswerable refusal | **9/16** | At least 13/16 |
| Held-out semantic-only positive retention | **7/8** | 8/8 |
| Hybrid admitted Recall@3, `generate_false` | **0.70**, from raw 0.85 | At least 0.85 |
| Hybrid admitted Recall@3, `generate_true` | **0.70**, from raw 0.85 | At least 0.85 |

The passing gates were held-out positive retention **15/16**, plus hybrid
unanswerable refusal **4/4**, structured exactness **6/6** and temporal exactness
**4/4** in each of the two generation settings. The 21 correctness checks cover
six execution checks, six citation-record checks, six typed-decision checks and
three cross-generation consistency checks; they are not a count of 21
semantically correct model answers. Document-ID coverage also does not prove
that every retrieved passage entails the answer.

The forward count is reconstructed as
`32 + 2 × (72 hybrid + 72 structured + 76 temporal) = 472`, not an instrumented
runtime counter. Admission candidate counts refer to passages/chunks, not
unique documents. Dividing by the verifier's elapsed time gives approximately
**0.2487 reconstructed forwards per wall-second**, including loading, retrieval
and evaluation work; this is not isolated inference throughput. `generate_true`
uses the extractive provider. Neither setting measures chat decoding, TTFT or
GGUF generation speed, and this single frozen synthetic replay does not establish
general model quality.

The historical report's 656.861 s and this replay's 1,897.829 s are not a
controlled comparison under matched host conditions. Their timing difference
does not establish a software regression or LiteLLM overhead; this native
replay does not use LiteLLM.

The [earlier 1,800-second timeout](../reports/qwen-admission-timeout-20261123.json)
is retained separately. It produced no complete report and has
`quality_result: null`; it is neither a completed replay nor an additional measured quality
failure. The later completed replay supplies the quality result above without
retuning thresholds or replacing the timeout evidence.

## Reproduction and preserved evidence

For a new live run, use the [gateway setup](LITELLM.md),
[Qwen acceptance prerequisites](QWEN_GATEWAY_SPEC.md#reproduce-the-live-acceptance),
and the RAG [pinned model/runtime preparation](https://github.com/MasterLKH180cm/RAGs/blob/0f72f4569e26528dc0c480baec7ad6320160d1ce/docs/SELF_HOSTED_QWEN_SPEC.md).
From the Agentic repository, with `AGENT_TEST_POSTGRES_URL` explicitly pointing
to a disposable test administrator endpoint:

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

Use a new output directory. `--smoke-only` omits the 192-request benchmark;
`--baseline-planner` optionally takes a trusted, reviewed local source file for
a same-model replay. Model weights, server binary and the isolated gateway must
already be prepared and verified. The full-system launcher creates separate
test databases and stops processes before removing them. Its raw response
capture is restricted to the dedicated public synthetic tenant; it is not a
production logging mode.

To execute a new **native admission** replay, use its separately prepared
environment and verified offline model cache. The runtime versions must match
the frozen [admission profile](https://github.com/MasterLKH180cm/RAGs/blob/0f72f4569e26528dc0c480baec7ad6320160d1ce/profiles/admission-qwen-v03.json).
From the recorded RAG source checkout, use explicit datasets and a new report
path; the profile fixes the threshold at 10.75:

```bash
python -m rags.verify_admission \
  --profile profiles/admission-qwen-v03.json \
  --holdout examples/admission_holdout_v03.json \
  --regression examples/eval_v02.json \
  --cache-dir /absolute/path/to/verified-native-qwen-cache \
  --report /absolute/path/to/new-native-admission-report.json \
  --enforce-quality
```

This performs native bfloat16 admission scoring; it does not execute the GGUF
generation benchmark. A quality-gate failure must remain a failed result even
when execution/correctness checks pass.

The standalone [performance analysis helper](../reports/qwen-evidence-20261123/analyze_performance.py)
can recompute the descriptive tables without model inference. Supply explicit
paths and a fresh output file:

```bash
python reports/qwen-evidence-20261123/analyze_performance.py \
  --input-dir reports/qwen-live-performance-20261123 \
  --output /tmp/qwen-performance-reproduction-new/analysis.json
```

A successful analysis exit means evidence parsing completed; the retained
benchmark status must still be `failed`. A new analysis timestamp or checkout
path can change regenerated bytes. Compare raw input hashes and numerical
results using the [provenance record](../reports/qwen-evidence-20261123/performance-provenance.json),
rather than requiring a byte-identical new analysis file.

The separate
[admission analysis helper](../reports/qwen-evidence-20261123/analyze_admission.py)
accepts explicit inputs without changing the frozen profile or original report:

```bash
python reports/qwen-evidence-20261123/analyze_admission.py \
  --rags-root /absolute/path/to/RAGs \
  --report /absolute/path/to/fresh-native-report.json \
  --output /absolute/path/to/new-admission-analysis.json
```

These helpers are evidence utilities retained with the reports, not new
production APIs. They do not turn fixture checks, historical measurements or
failed quality gates into new model-success evidence.
