# Qwen measurement evidence: 2026-11-23

This directory preserves standalone analysis tools for the fixed measurement
reports from this evaluation. These are evidence-reproduction utilities, not
production services or a general benchmark API. Both analyzers use only the
Python standard library and refuse to overwrite an existing output file.

Run the commands below from the `agentic-system` repository root, with the
companion `RAGs` checkout next to it. Explicit input and output arguments avoid
relying on the original operator's scratch-directory defaults. Choose a new
output filename for each reproduction.

## Generation measurements

```bash
python reports/qwen-evidence-20261123/analyze_performance.py \
  --input-dir reports/qwen-live-performance-20261123 \
  --output /tmp/qwen-performance-reproduction-20261123-01.json
```

[The provenance manifest](performance-provenance.json) binds the analyzer, the
original derived analysis, the four raw reports, and the measured source
revisions. It also distinguishes the measured system harness from the later,
AST-equivalent parenthesis clarification.

The analyzer checks case/repetition completeness, recomputes all-attempted and
passed-only latency separately, and verifies the reported counts and identity
fields. Successful analysis means the evidence is internally consistent. It
does **not** turn a failed model contract into a pass: the preserved generation
gate is failed, `comparison_permitted` is false, and no speedup is computed.

The diagnostic reports use six synthetic case families. Repetition counts are
not independent general-knowledge questions. Raw benchmark samples record
validation outcomes and usage, not response bodies; the separate diagnostic
probe supplies one observed response for the failing grounded-answer case.

## Frozen native admission replay

```bash
python reports/qwen-evidence-20261123/analyze_admission.py \
  --rags-root ../RAGs \
  --report reports/qwen-admission-replay-20261123.json \
  --output /tmp/qwen-admission-reproduction-20261123-01.json
```

Use the recorded RAG source revision when reproducing the analysis. The helper
validates the frozen profile and datasets, the complete two-generation by
three-mode report shape, case sets, gate summaries, promotion status and exit
policy. It keeps native admission scoring distinct from GGUF text generation.

[The native provenance manifest](native-provenance.json) binds the complete
replay, its resource record, derived analysis, analyzer, original operator
wrapper and progress log. It retains the failed quality/promotion result and
distinguishes the earlier incomplete timeout from this completed measurement.

The number of model forwards is reconstructed from the scorer's one-forward
per passage behavior and recorded admission candidate counts; it is not an
instrumented model-call counter. The analysis records its own source hash and
the RAG source hashes observed at analysis time. Those hashes are an
analysis-time snapshot, not retroactive proof of what executed. The replay
resource report separately records the operator's execution-time source
snapshot and raw-report hash.

The original operator wrapper is preserved as `replay_operator.py` for
provenance. Its absolute paths describe this particular run. To execute a new
model replay, use the public `rags.verify_admission` CLI with a new report path
and the pinned offline model cache, as documented in the main results report.

## Interpreting a reproduction

New analysis timestamps, checkout paths and helper provenance make regenerated
JSON files differ byte for byte from an earlier analysis. Compare input hashes,
identities, gate outcomes, counts and metrics. Never overwrite the original
measurement to make a reproduction appear identical. A timeout or partial log
does not replace a complete verification report.
