# Self-hosted Inkling 2 Mini implementation contract

Inkling 2 Mini is distributed with downloadable Apache-2.0 weights. Preserve the
existing hosted `inkling` preset and provide an explicit `inkling-local` route
preset for a user-run OpenAI-compatible model server.

## Configuration

- Add route `auth_mode` with `required` as the backwards-compatible default and
  `none` as an explicit local choice. `none` is valid only for OpenAI-compatible
  routes on clean literal-loopback HTTP(S) endpoints; reject DNS names, private
  LAN/public addresses and contradictory keys or `api_key_env` settings.
- The local preset uses `thinkingmachines/Inkling-2-Mini`, an explicit
  `http://127.0.0.1:8080/v1` default, no reasoning-effort override, and the
  `max_tokens` output field. The existing hosted preset retains Tinker semantics.
- Add a validated output-token-field choice with the current hosted behavior as
  the default. Local authenticated routes require a dedicated explicit key name;
  never inherit ambient Tinker/OpenAI keys merely because the model is Inkling.
- No-auth requests omit Authorization and bypass ambient proxies. Keep current
  retry/fallback/deadline/usage handling, body limits, refusal/truncation checks,
  strict JSON plans, leases, budgets and approval contracts intact.
- Container clients need an explicit host/service endpoint with required auth;
  document that their loopback points to the container itself.

## Evidence and testing

Document the RAGs pinned local-serving and benchmark path. The same model name
does not establish identical weights, quantization, runtime or performance.
Require explicit provenance when comparing local benchmark results. Preserve
prior hosted missing-credential reports as historical evidence, and explain that
they do not block local deployment.

Use an actual loopback HTTP server to verify unauthenticated local inference,
wire fields, absence of Authorization/proxy leakage, no ambient-secret loading,
and refusal of unsafe endpoints/configurations. Run core tests and the existing
real two-service/process acceptance against the matching updated RAG revision.

Primary sources checked 2026-11-23:

- https://thinkingmachines.ai/model-card/inkling-2-mini/
- https://huggingface.co/ggml-org/Inkling-2-Mini-GGUF
- https://unsloth.ai/docs/models/inkling-2-mini.md
