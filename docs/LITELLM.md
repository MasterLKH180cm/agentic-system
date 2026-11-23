# Shared LiteLLM generation gateway

The optional gateway gives RAGs and Agentic one generation endpoint while the
applications retain their own authorization and durable state. The default
Compose deployment remains credential-free for model inference. This guide
implements [LITELLM_SPEC.md](LITELLM_SPEC.md).

## What is connected

| Path | Request contract | Responsibility retained by application |
| --- | --- | --- |
| RAG generation | RAG router → verified HTTPS → LiteLLM chat completions | Evidence packing, citations, tenant/version scope |
| RAG native agent | Same endpoint, validated function definitions/results | Tool authorization, approvals, output validation and atomic conversation finalization |
| Agentic planning/synthesis | Explicit OpenAI-compatible route, bounded JSON/text | Workflow state, reservations, lease fencing, cancellation and result validation |
| Embeddings | Existing separately configured provider | Immutable embedding-space identity and reindex compatibility |

The default generation alias is `gateway/inkling-2-mini-iq4nl-v1`, using
`max_tokens` with no reasoning-effort override. It maps to the operator's
`thinkingmachines/Inkling-2-Mini` inference server. The alias is a deployment
contract, not proof of the loaded weights or tool parser. Retain the exact
model, runtime and quantization manifest; use a new alias if they change.
Contextual, hierarchy and graph-derived indexes also depend on generation
identity, so do not change an existing alias's meaning in place.
The optional [Qwen deployment](#select-qwen3-17b) has its own immutable alias and
fresh service credentials; it does not change the default Inkling selection.

Only generation is included. An arbitrary embedding alias, even one with the
same vector dimensions, cannot replace an existing embedding space. Changing
the router endpoint or `RAGS_EMBEDDING_SPACE_ID` requires reindexing. Existing
research adapters retain their own tokenizer/model restrictions.

## Version and isolation

The deployment pins LiteLLM **1.110.2**, including the OCI index digest in
[`runtime.json`](../deploy/litellm/runtime.json). It never pulls a floating
`latest` or `main-stable` tag. The official release-specific image verification
instructions are at <https://github.com/BerriAI/litellm/releases/tag/v1.110.2>.
The independent Linux/Python test runtime is hash-locked in
[`requirements.txt`](../deploy/litellm/requirements.txt); the application does
not import the LiteLLM SDK or inherit its dependencies.

The gateway has a separate PostgreSQL database. Its master key, salt, upstream
credential and database password are not mounted into RAGs, the Agent API or
the worker. An administrative one-shot initializer registers distinct service
keys, restricted to the single model alias and four model/chat routes. It
verifies their active status and policy on restart. A deleted, revoked,
expired or changed key fails initialization; the initializer does not restore
its access. Receipts publish atomically without overwriting existing files.

The application keys identify services. They do not establish per-tenant
gateway budgets. Tenant authorization remains in each application and
Agentic still reserves and reconciles usage per run. Add tenant-key mapping
only with an explicit server-side identity and quota design.

## Start the full stack

Requirements: sibling `RAGs` and `agentic-system` checkouts, OpenSSL, Docker with
Compose supporting `include` (2.20 or later), and a separately provisioned
inference server. No model weights are downloaded by this setup.

For a new installation, from `agentic-system`:

```bash
python scripts/bootstrap_compose.py
python scripts/bootstrap_litellm.py
docker compose -f compose.yaml -f compose.litellm.yaml --profile litellm up -d --build --wait
```

Keep existing `.env` and application configuration when upgrading. Both
bootstrap commands refuse to overwrite existing files. The gateway bootstrap
creates `data/litellm`, a host-private directory containing separate database,
administrative, upstream and service credentials. It also creates a private
CA and a gateway certificate valid for `litellm`, `localhost`, `127.0.0.1` and
`::1`. Application containers mount only their own service credential and CA
directory, read-only. The CA signing key is never mounted into a container.

The one-shot `litellm-init` administrator receives the master credential through
its private environment file. Its only writable bind mount is the private
`data/litellm/receipts` directory; CA certificate and the two service-key files
are mounted individually read-only. It receives no CA signing key, gateway
server key, upstream credential or database configuration. The initializer runs
as UID 0 with all capabilities dropped except `DAC_OVERRIDE`, needed to traverse
and write the operator-owned `0700` receipt directory; `no-new-privileges` stays
enabled. This exception applies only to the one-shot administrator.

The default upstream is `http://host.docker.internal:8080/v1`. Configure that
inference server to require `data/litellm/upstream.key`, using its supported
key-file option. On Linux, the host server must listen on an address reachable
from the Docker bridge; a host-only loopback listener is not reachable through
the bridge address. A separately managed inference service can be selected
before bootstrap:

```bash
python scripts/bootstrap_litellm.py --upstream-url https://inference.example/v1
```

Set `INKLING_UPSTREAM_API_KEY` in the private `data/litellm/proxy.env` to that
server's credential if it is already configured. Never put the credential in
the URL or commit the generated files. Configure the upstream's trusted CA
separately if its certificate is private; the shipped CA is for client-to-
gateway TLS. Keep the upstream fixed to the approved model deployment.

The default gateway port is published only at `127.0.0.1:4000`. PostgreSQL has
no host port. Health checks query gateway readiness, not model generation.
Application startup waits for the gateway and restricted-key initialization.
Unavailable weights or an unreachable inference server still cause explicit
model-call failure; a healthy gateway does not prove inference readiness.

For a gateway shared with separately deployed applications:

```bash
docker compose -f deploy/litellm/compose.yaml --profile litellm up -d --build --wait
```

Only one gateway stack should own this database and port. Do not start the
standalone and included full-stack copies together. A hosted domain needs a
certificate valid for that name and an appropriate network binding. Install
the operator's server certificate/key in the private gateway paths and provide
the corresponding trust bundle to clients; do not disable TLS verification.

## Select Qwen3-1.7B

The optional Qwen preset connects the official Qwen3-1.7B Q8_0 deployment through
`gateway/qwen3-1.7b-q8_0-v1`. Its upstream must serve the exact configured name
`qwen/Qwen3-1.7B`; LiteLLM uses `openai/qwen/Qwen3-1.7B`. See
[QWEN_GATEWAY_SPEC.md](QWEN_GATEWAY_SPEC.md) and the companion RAG repository's
`docs/SELF_HOSTED_QWEN_SPEC.md` for the pinned artifact/runtime and CPU recipe.

Run these commands only in a fresh deployment directory containing the sibling
checkouts, with no existing `data/litellm`. For an existing Inkling installation,
use a separate checkout/deployment directory, new keys, and a distinct gateway
database volume. Both bootstrap commands refuse to overwrite existing files.
Do not reuse Inkling service keys or change the alias inside existing provisioning
receipts: the initializer rejects that change instead of expanding a key's access.

```bash
python scripts/bootstrap_compose.py
python scripts/bootstrap_litellm.py --model qwen
docker compose -p agentic-qwen \
  -f compose.yaml -f compose.litellm.yaml -f compose.qwen.yaml \
  --profile litellm up -d --build --wait
```

`compose.qwen.yaml` must be the final application overlay. It selects the same
single Qwen alias for RAG generation, API planning/synthesis, and worker execution.
The bootstrap supplies that alias to the administrative initializer through
`LITELLM_MODEL_ALIAS`; applications receive their own restricted keys. Hashing
embeddings, application authorization, TLS, and zero gateway retry/fallback/cache
settings retain their existing behavior. The explicit Compose project name gives
the new gateway its own database volume; the default host port is still 4000,
so do not run two gateway stacks on that port simultaneously.

Qwen bootstrap writes `QWEN_UPSTREAM_API_BASE` and `QWEN_UPSTREAM_API_KEY` in its
private `data/litellm/proxy.env`. The inference server must require the generated
`data/litellm/upstream.key`, or the private proxy configuration must use the
credential of an independently provisioned server. As with Inkling, a server
reached through Docker's host bridge needs an explicit reachable binding and
authentication. The direct loopback Qwen preparation recipe defaults to no auth;
it must not be exposed to the bridge without an explicitly configured server key.

For a separately deployed gateway, bootstrap the fresh directory with
`--model qwen`, then use the existing standalone command:

```bash
docker compose -p agentic-qwen -f deploy/litellm/compose.yaml \
  --profile litellm up -d --build --wait
```

The standalone services obtain the chosen model from generated proxy/admin
configuration; they do not need the application-only `compose.qwen.yaml` overlay.
Omitting `--model` from bootstrap continues to select Inkling. Only one of the
standalone or full-stack commands should own a given gateway database and port.

## Application configuration

The full-stack overlay wires all of these settings automatically. A separately
deployed RAG uses its existing router:

```dotenv
RAGS_ENV=production
RAGS_MODEL_PROVIDER=router
RAGS_GENERATION_MODEL=inkling
RAGS_ROUTER_ENABLED=true
RAGS_ROUTER_GATEWAY_URL=https://litellm:4000/v1
RAGS_ROUTER_AUTH_MODE=required
RAGS_ROUTER_API_KEY_FILE=/app/gateway/api.key
RAGS_ROUTER_CA_FILE=/app/gateway/ca.pem
RAGS_ROUTER_ALLOW_HTTP=false
RAGS_ROUTER_MODELS_JSON='{"inkling":"gateway/inkling-2-mini-iq4nl-v1"}'
RAGS_ROUTER_MODEL_OPTIONS_JSON='{"inkling":{"protocol":"openai"}}'
RAGS_GENERATION_TOKENIZER_JSON='{"backend":"utf8_bytes"}'
RAGS_CONTEXT_BUDGET=4096
RAGS_OUTPUT_BUDGET=1024
```

`rags init --profile litellm` creates a credential-private starter profile.
Its gateway URL/key/mapping are deliberately empty until an operator configures
them together; no external call is authorized by selecting the profile alone.
The existing `inkling-local` profile remains available as a direct baseline.

Agentic uses [`examples/litellm.routes.json`](../examples/litellm.routes.json)
as `AGENT_MODEL_ROUTES_JSON`, with `AGENT_MODEL_PROVIDER=openai` and
`AGENT_GATEWAY_API_KEY_FILE=/app/gateway/api.key`. Route transport is `openai`;
the model field is the gateway alias. Do not set the environment selector to
`routed`: the API uses that resolved label for explicit routes selected from
the offline default, but the environment selector has a narrower contract.

Custom CA files must be bounded regular PEM files and cannot be symlinks.
RAG captures the CA bytes/hash when configuration loads; changing its trust
root invalidates pending native-agent approvals. Changing the gateway URL or
model alias also changes the approval execution fingerprint. Recreate clients
after intentional trust/configuration changes and obtain a fresh approval.

## Retries, usage and logs

The shipped gateway has one deployment per alias, no cache, no fallback,
zero router retries and zero provider-SDK retries. Agentic owns its existing
bounded route/job retry policy. Both applications retain their own whole-call
deadlines, including response headers and body reads. Cancelling a client wait
cannot guarantee that a remote model stops computing; uncertain usage remains
conservatively charged by the application.

Enable gateway fallback only after defining how every upstream attempt's usage
and cost will be reconciled. Applying one route's price to a different fallback
model is invalid. Local inference has no fabricated per-token monetary price:
Agentic reports unknown cost unless the operator supplies prices. A gateway
display of zero for an unknown model is not evidence that computation is free.

Prompt/response storage, error-body logging, external callbacks, debug logging
and telemetry are disabled in the supplied configuration. Metadata/spend
tracking remains database-backed. Do not enable private reasoning or source
content logging to diagnose a routing issue. The acceptance harness checks
synthetic prompt and reasoning markers for accidental publication/logging.

## Verification and direct/proxy comparison

With the development environment and a Compose binary installed, verify actual
configuration merging for both model selections without starting containers:

```bash
.venv/bin/python scripts/check_litellm_compose.py \
  --report reports/testing/litellm-compose-inkling.json
.venv/bin/python scripts/check_litellm_compose.py --model qwen \
  --report reports/testing/litellm-compose-qwen.json
```

`--compose-binary /absolute/path/to/docker-compose` selects a standalone Compose
executable. The checker creates disposable private bootstrap files, renders the
full stack and standalone gateway with the actual CLI, and verifies that proxy,
initializer, RAG, API and worker agree on one selected alias. It also checks TLS,
read-only service-key mounts, administrative secret isolation and startup gates.
It never prints or retains rendered secrets, starts a container, or calls a model.

The isolated process acceptance starts the real pinned LiteLLM executable,
real PostgreSQL, verified TLS and a deterministic local provider. It tests
production application adapters, native RAG agent behavior, usage, virtual-key
restrictions, truncation, deadlines and actual upstream attempt counts:

```bash
uv venv /tmp/litellm-check --python 3.12
uv pip install --python /tmp/litellm-check/bin/python --require-hashes \
  -r deploy/litellm/requirements.txt
LITELLM_LOCAL_MODEL_COST_MAP=True /tmp/litellm-check/bin/python -m prisma version
gateway_schema=$(/tmp/litellm-check/bin/python -c \
  "import importlib.util,pathlib; print(pathlib.Path(importlib.util.find_spec('litellm').origin).parent / 'proxy/schema.prisma')")
PATH="/tmp/litellm-check/bin:$PATH" LITELLM_LOCAL_MODEL_COST_MAP=True \
  /tmp/litellm-check/bin/python -m prisma generate --schema "$gateway_schema"
AGENT_TEST_POSTGRES_URL=postgresql://testoperator@127.0.0.1/disposable \
  .venv/bin/python scripts/check_litellm.py \
  --gateway-python /tmp/litellm-check/bin/python \
  --rags-path ../RAGs --report reports/testing/litellm-process.json
```

Use a disposable PostgreSQL server whose role can create databases. The
harness creates a UUID database and drops only that database. Prisma may need
to download its pinned platform engines during preparation. Generate its Python
client from the installed LiteLLM schema before starting the harness; checking
`prisma version` alone does not generate that client. The actual proxy
serves with environment proxies stripped. The container image bundles its
runtime separately. These are protocol and persistence tests with synthetic
responses, not Inkling quality or performance measurements.

The [process acceptance report](../reports/litellm-process-acceptance.json) records
that fixture scope. The [review report](REVIEW_LITELLM.md) and
[local validation record](testing-results-review-litellm.json) separately track
application regressions and their tested source snapshot. Local process evidence
does not establish container CI status or real-model performance.

On hardware able to load the actual pinned model, run the RAG benchmark twice
with the same deployment manifest, prompt set, context, slots, threads,
warmups and concurrency. From `RAGs`:

```bash
python -m rags.model_benchmark run --preset inkling-local \
  --deployment-manifest deployment.json --output reports/direct.json
python -m rags.model_benchmark run --preset inkling-litellm \
  --ca-file ../agentic-system/data/litellm/ca.pem \
  --deployment-manifest deployment.json --output reports/proxy.json
python -m rags.model_benchmark compare reports/direct.json reports/proxy.json \
  --require-same-deployment --output reports/gateway-comparison.json
```

Supply the proxy benchmark credential through
`LITELLM_BENCHMARK_API_KEY_FILE`, pointing to the RAG service key; an operator
can instead issue a separate model-restricted benchmark key. An authenticated
direct inference baseline must also use `--auth-mode required` and its dedicated
`INKLING_LOCAL_API_KEY_FILE`. Do not send the gateway key to the upstream.

The comparison requires two passing live reports, identical complete deployment
manifests, protocol, workload and client runtime. It reports latency percentiles
and validated throughput. No streaming is used, so it does not report TTFT or
decoder-only tokens per second. Operator manifests do not independently attest
to the served weights. The known execution host cannot hold the selected
17,627,940,768-byte Inkling artifact plus runtime memory; the integration does
not change that hardware boundary.

For the actual Qwen deployment, use `qwen-local` and `qwen-litellm` in the same
benchmark commands and `QWEN_LOCAL_API_KEY_FILE` for an authenticated direct
baseline. `LITELLM_BENCHMARK_API_KEY_FILE` remains the gateway credential selector.
Keep the same running upstream process, deployment manifest and workload for both
paths. The Qwen Q8_0 generation measurements are distinct from the historical
bfloat16 answerability benchmark; they cannot promote its frozen quality profile.

## Backup, renewal and rollback

Back up the gateway database and private configuration together. Do not delete
provisioning receipts to revive a revoked key. Rotate keys deliberately through
the administrative API, update the relevant service file and receipt policy,
then recreate only the affected applications. The server certificate expires
after 90 days and the generated CA after 365 days; renew before expiry. A CA
change requires updated client trust and new pending RAG approvals.

To return to direct/offline generation, recreate the application stack using
the original `compose.yaml` and the previous explicit provider settings. Stop
the three gateway services after clients stop using them. Retain their volume
and private configuration for audit/rollback; do not use `down -v` as an
upgrade or rollback procedure. RAG and Agentic databases remain independent.
