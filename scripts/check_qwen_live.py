"""Actual pinned Qwen CPU inference through RAG, LiteLLM and independent workers.

Requires a verified local artifact/runtime and an explicitly disposable PostgreSQL
administrative URL (AGENT_TEST_POSTGRES_URL). No hosted model is contacted. All
model requests use public synthetic data. Failures remain in a new report directory.
"""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import resource
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import traceback

import httpx
import psycopg

from check_litellm import (
    AcceptanceFailure, environment, free_port, load_module, read_env, require,
    stop_process, temporary_gateway_database,
)
from qwen_acceptance import ARCHIVE_TEXT, validate_rag_generation, validate_workflow

ROOT = Path(__file__).resolve().parents[1]
ALIAS = 'gateway/qwen3-1.7b-q8_0-v1'
MODEL = 'qwen/Qwen3-1.7B'


def write_new(path, value):
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.write('\n')


def safe_environment():
    inherited = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'XDG_CACHE_HOME', 'TIKTOKEN_CACHE_DIR',
                 'PRISMA_QUERY_ENGINE_BINARY', 'PRISMA_SCHEMA_ENGINE_BINARY'}
    return {name: value for name, value in os.environ.items() if name in inherited}


def start(stack, processes, directory, name, command, env, cwd=ROOT):
    log = stack.enter_context((directory / (name + '.log')).open('wb'))
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    processes.append((name, process))
    stack.callback(stop_process, process)
    return process


def ready(client, process, path, *, seconds=100):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        require(process.poll() is None, 'service_exited_before_readiness')
        try:
            if client.get(path, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    raise AcceptanceFailure('service_readiness_timeout')


def process_memory(pid):
    values = {}
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith(('VmRSS:', 'VmHWM:')):
            name, amount, _ = line.split()
            values[name.rstrip(':') + '_bytes'] = int(amount) * 1024
    return values


def collect_synthetic_trace(report, directory):
    """Best-effort diagnostics never replace the actual application result."""
    capture = {'status': 'unavailable', 'reported_capture_failures': 0}
    report['synthetic_trace_capture'] = capture
    try:
        trace = directory / 'worker-trace.jsonl'
        if trace.exists():
            with trace.open('rb') as source:
                data = source.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                capture.update(status='incomplete', failure='trace_size_limit')
            else:
                lines = data.decode('utf-8').splitlines()
                if len(lines) > 128:
                    capture.update(status='incomplete', failure='trace_record_limit')
                else:
                    report['synthetic_worker_trace'] = [json.loads(line) for line in lines]
                    capture['status'] = 'captured'
        log_path = directory / 'worker.log'
        if log_path.exists():
            with log_path.open('rb') as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - 65536))
                tail = source.read(65536).decode('utf-8', errors='replace')
            failures = [int(value) for value in re.findall(r'QWEN_CAPTURE_FAILURE count=(\d{1,9})\b', tail)]
            capture['reported_capture_failures'] = max(failures, default=0)
            if failures:
                capture['status'] = 'incomplete'
    except Exception as exc:
        capture.update(status='incomplete', error_type=type(exc).__name__)


def planner_replay(report, directory, endpoint, upstream_key, baseline):
    from agentic.config import ModelRoute, Settings
    from agentic.model import ModelGateway
    from agentic.planner import plan
    from agentic.store import Store

    route = ModelRoute('qwen-direct', 'openai', MODEL, endpoint, upstream_key,
                       output_token_parameter='max_tokens', supports_json_schema=True)
    config = Settings(db_path=str(directory / 'planner.db'), keys={'test': 'synthetic'},
                      rag_url='http://127.0.0.1:1', rag_keys={'synthetic': 'test'}, provider='routed',
                      openai_key='', openai_base_url='', openai_model='', anthropic_key='',
                      anthropic_model='', max_parallel=1, routes=(route,), max_model_output_tokens=512,
                      max_model_tokens=30000)
    store = Store(config.db_path)

    class RecordingGateway(ModelGateway):
        async def _request(self, *args, **kwargs):
            self.request_attempted = True
            reply = await super()._request(*args, **kwargs)
            self.http_status = reply.status_code
            if reply.status_code == 200:
                payload = reply.json()
                usage = payload.get('usage')
                if isinstance(usage, dict):
                    self.reported_usage = {key: value for key, value in usage.items()
                        if key in {'prompt_tokens', 'completion_tokens', 'total_tokens'}
                        and type(value) is int and value >= 0}
                choices = payload.get('choices') or []
                if choices:
                    self.raw_output = choices[0].get('message', {}).get('content')
            return reply

        async def complete(self, system, message, **kwargs):
            self.prompt_sha256 = hashlib.sha256((system + '\n' + message).encode()).hexdigest()
            schema = kwargs.get('response_schema')
            self.schema_sha256 = (hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(',', ':'),
                                                          allow_nan=False).encode()).hexdigest()
                                  if schema is not None else None)
            self.output = await super().complete(system, message, **kwargs)
            return self.output

    gateway = RecordingGateway(config, store)
    goal = 'Find the sample retention period, retrieve at most 2 results, then summarize the evidence.'
    specification = {'allowed_tools': ['rag.search', 'model.summarize'], 'allowed_namespaces': ['default'],
                     'memory_namespace': 'default', 'instructions': '', 'runtime_mode': 'dag'}
    variants = [('current', plan)]
    if baseline is not None:
        variants.insert(0, ('baseline', load_module('qwen_baseline_planner', baseline).plan))
        report['baseline_planner_sha256'] = hashlib.sha256(baseline.read_bytes()).hexdigest()
    report['planner_replay'] = []
    for name, function in variants:
        started = time.perf_counter()
        item = {'variant': name}
        gateway.output = None
        gateway.raw_output = None
        gateway.prompt_sha256 = None
        gateway.schema_sha256 = None
        gateway.request_attempted = False
        gateway.http_status = None
        gateway.reported_usage = {}
        try:
            steps = asyncio.run(function(goal, gateway, specification=specification, tenant='local'))
            retrievals = [step for step in steps if step.tool == 'rag.search']
            summaries = [step for step in steps if step.tool == 'model.summarize']
            retrieval_ids = {step.id for step in retrievals}
            valid = bool(retrievals and summaries
                         and all(step.args.get('top_k', 5) <= 2 for step in retrievals)
                         and any(retrieval_ids.intersection(step.depends_on) for step in summaries))
            item.update(valid=valid, steps=[step.model_dump() for step in steps])
        except Exception as exc:
            item.update(valid=False, error_type=type(exc).__name__)
        item.update(elapsed_seconds=round(time.perf_counter() - started, 6),
                    prompt_sha256=getattr(gateway, 'prompt_sha256', None),
                    response_schema_sha256=getattr(gateway, 'schema_sha256', None),
                    request_attempted=gateway.request_attempted,
                    http_status=gateway.http_status,
                    reported_usage=gateway.reported_usage,
                    inference_confirmed=(gateway.http_status == 200 and (
                        gateway.reported_usage.get('completion_tokens', 0) > 0
                        or isinstance(gateway.raw_output, str) and bool(gateway.raw_output))),
                    output=getattr(gateway, 'output', None),
                    raw_output=getattr(gateway, 'raw_output', None))
        report['planner_replay'].append(item)
    require(report['planner_replay'][-1]['valid'], 'current_planner_invalid_with_real_qwen')


def system_workflows(report, stack, processes, directory, args, origin, private, rag_database, agent_database):
    rag_port, agent_port = free_port(), free_port()
    while agent_port == rag_port:
        agent_port = free_port()
    rag_url, agent_url = f'http://127.0.0.1:{rag_port}', f'http://127.0.0.1:{agent_port}'
    rag_key, agent_key = 'rag-' + secrets.token_hex(24), 'agent-' + secrets.token_hex(24)
    tenant = 'qwen-live-synthetic'
    route = {'name': 'qwen-gateway-v1', 'provider': 'openai', 'model': ALIAS, 'base_url': origin + '/v1',
             'api_key_env': 'AGENT_GATEWAY_API_KEY', 'auth_mode': 'required',
             'ca_file': str(private / 'agentic/ca.pem'), 'output_token_parameter': 'max_tokens',
             'supports_json_schema': True,
             'purposes': ['planning', 'synthesis']}
    env = safe_environment()
    env.update({
        'NO_PROXY': '127.0.0.1,localhost', 'PYTHONPATH': str(args.rags_path) + os.pathsep + str(ROOT),
        'RAGS_ENV': 'test', 'RAGS_DB_PATH': str(directory / 'rags.db'),
        'RAGS_DATABASE_URL': rag_database,
        'RAGS_KEYS_JSON': json.dumps({rag_key: tenant}), 'RAGS_MODEL_PROVIDER': 'router',
        'RAGS_EMBEDDING_PROVIDER': 'local', 'RAGS_ADMISSION_POLICY': 'compatibility',
        'RAGS_GENERATION_MODEL': 'qwen', 'RAGS_ROUTER_GATEWAY_URL': origin + '/v1',
        'RAGS_ROUTER_API_KEY_FILE': str(private / 'rags/api.key'),
        'RAGS_ROUTER_CA_FILE': str(private / 'rags/ca.pem'), 'RAGS_ROUTER_AUTH_MODE': 'required',
        'RAGS_ROUTER_MODELS_JSON': json.dumps({'qwen': ALIAS}),
        'RAGS_ROUTER_MODEL_OPTIONS_JSON': json.dumps({'qwen': {'protocol': 'openai'}}),
        'RAGS_GENERATION_TOKENIZER_JSON': json.dumps({'backend': 'utf8_bytes'}),
        'RAGS_CONTEXT_BUDGET': '4096', 'RAGS_OUTPUT_BUDGET': '512',
        'AGENT_ENV': 'test', 'AGENT_DB_PATH': str(directory / 'agent.db'),
        'AGENT_DATABASE_URL': agent_database,
        'AGENT_KEYS_JSON': json.dumps({agent_key: tenant}), 'AGENT_RAG_KEYS_JSON': json.dumps({tenant: rag_key}),
        'AGENT_MODEL_PROVIDER': 'openai', 'AGENT_RAG_URL': rag_url,
        'AGENT_GATEWAY_API_KEY_FILE': str(private / 'agentic/api.key'),
        'AGENT_MODEL_ROUTES_JSON': json.dumps([route]), 'AGENT_RAG_ADMISSION_POLICY': 'compatibility',
        'AGENT_ARTIFACT_DIR': str(directory / 'artifacts'), 'AGENT_RATE_DB_PATH': str(directory / 'rate.db'),
        'AGENT_RATE_PER_MINUTE': '3000', 'AGENT_MAX_PARALLEL': '1', 'AGENT_MAX_ATTEMPTS': '1',
        'AGENT_MAX_MODEL_OUTPUT_TOKENS': '512', 'AGENT_MAX_MODEL_TOKENS': '30000',
        'AGENT_MODEL_TIMEOUT_SECONDS': '90', 'AGENT_STEP_TIMEOUT_SECONDS': '180',
    })
    rag_process = start(stack, processes, directory, 'rags', [str(args.rags_python), '-m', 'uvicorn',
                        'rags.api:app', '--host', '127.0.0.1', '--port', str(rag_port)], env, args.rags_path)
    agent_process = start(stack, processes, directory, 'api', [sys.executable, '-m', 'uvicorn',
                          'agentic.api:app', '--host', '127.0.0.1', '--port', str(agent_port)], env)
    start(stack, processes, directory, 'worker', [sys.executable, str(ROOT / 'scripts/qwen_live_worker.py'),
          '--synthetic-trace', str(directory / 'worker-trace.jsonl')], env)
    with httpx.Client(trust_env=False, timeout=100) as client:
        ready(client, rag_process, rag_url + '/live')
        ready(client, agent_process, agent_url + '/live')

        def request(method, base, path, key, payload=None, expected=200):
            reply = client.request(method, base + path, headers={'Authorization': 'Bearer ' + key}, json=payload)
            require(reply.status_code == expected, 'application_http_' + str(reply.status_code))
            return reply.json()

        request('POST', rag_url, '/documents', rag_key,
                {'document_id': 'sample-archive', 'title': 'Sample archive policy',
                 'text': ARCHIVE_TEXT,
                 'chunking': {'strategy': 'fixed', 'max_tokens': 256, 'overlap_tokens': 0},
                 'tokenizer': {'backend': 'unicode'}}, 201)
        started = time.perf_counter()
        answer = request('POST', rag_url, '/query', rag_key,
                         {'question': 'How long are sample archive exports retained?', 'mode': 'hybrid',
                          'top_k': 1, 'generate': True})
        report['rag_generation'] = {'elapsed_seconds': round(time.perf_counter() - started, 6),
                                    'outcome': answer.get('outcome'), 'answer': answer.get('answer'),
                                    'usage': answer.get('usage'),
                                    'citations': answer.get('citations'),
                                    'citation_count': len(answer.get('citations', [])),
                                    'citation_document_ids': [c['document_id'] for c in answer.get('citations', [])]}
        try:
            report['rag_generation']['acceptance'] = validate_rag_generation(answer)
        except ValueError as exc:
            raise AcceptanceFailure(str(exc)) from None
        request('POST', agent_url, '/registry/agents', agent_key,
                {'name': 'qwen-research', 'version': 1,
                 'instructions': 'Search the knowledge source and summarize its answer with source citations.',
                 'allowed_tools': ['rag.search', 'model.summarize'], 'evaluator': 'evidence', 'max_rounds': 2}, 201)
        jobs = [
            ('explicit_arithmetic', {'goal': 'Calculate 19 * 23 and report the result.', 'steps': [
                {'id': 'calculate', 'tool': 'math.calculate', 'args': {'expression': '19 * 23'}},
                {'id': 'answer', 'tool': 'model.summarize', 'depends_on': ['calculate']}]}),
            ('planned_retrieval', {'goal': 'How long are sample archive exports retained?',
                                  'agent_id': 'qwen-research', 'agent_version': 1}),
            ('adaptive_retrieval', {'goal': 'How long are sample archive exports retained?',
                                   'agent_id': 'qwen-research', 'agent_version': 1,
                                   'runtime_mode': 'agent_loop', 'max_rounds': 2}),
        ]
        report['workflows'] = []
        for name, payload in jobs:
            payload['max_model_tokens'] = 30000
            started = time.perf_counter()
            created = request('POST', agent_url, '/workflows', agent_key, payload, 202)
            deadline = time.monotonic() + 360
            while True:
                state = request('GET', agent_url, '/workflows/' + created['id'], agent_key)
                if state['status'] in {'completed', 'failed', 'cancelled'}:
                    break
                require(time.monotonic() < deadline, 'workflow_completion_timeout')
                time.sleep(.1)
            item = {'case': name, 'status': state['status'], 'run_id': state['id'], 'tenant': state['tenant'],
                    'elapsed_seconds': round(time.perf_counter()-started, 6),
                    'usage': state.get('usage'), 'steps': [{key: step.get(key) for key in
                     ('id', 'tool', 'status', 'attempts', 'depends_on', 'args', 'checkpoint', 'result', 'error')}
                     for step in state['steps']]}
            report['workflows'].append(item)
            try:
                item['acceptance'] = validate_workflow(state, name)
            except ValueError as exc:
                raise AcceptanceFailure(str(exc)) from None
            print('Live workflow: ' + name + ' passed', flush=True)


def check(args):
    require(not args.output.exists(), 'report_directory_exists')
    args.output.mkdir(parents=True)
    report = {'schema_version': 'qwen-live-system-v1', 'evidence_kind': 'actual_local_qwen_generation',
              'measured_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'limitations': ['Synthetic public workload; no general model-quality claim.',
                'Frozen bfloat16 admission is a separate evaluation and is not enabled in these generation flows.',
                'Non-streaming measurements do not measure TTFT or decoder-only throughput.',
                'One server slot; higher client concurrency includes queueing.']}
    processes = []
    started = time.perf_counter()
    try:
        require(1 <= args.repetitions <= 10, 'invalid_repetitions')
        require(1 <= args.threads <= 8, 'invalid_threads')
        args.rags_path = args.rags_path.resolve(strict=True)
        sys.path.insert(0, str(args.rags_path))
        sys.path.insert(0, str(args.rags_path / 'scripts'))
        import qwen_local
        from rags.model_benchmark import Options, DeploymentManifest, compare_reports, run_benchmark
        admin_url = os.environ.get('AGENT_TEST_POSTGRES_URL', '')
        require(bool(admin_url), 'missing_disposable_postgres_url')
        version = subprocess.run([str(args.gateway_python), '-c',
                                  'import importlib.metadata; print(importlib.metadata.version("litellm"))'],
                                 capture_output=True, text=True, timeout=30, check=True)
        require(version.stdout.strip() == '1.110.2', 'unexpected_litellm_runtime_version')
        report['gateway_version'] = version.stdout.strip()
        bootstrap = load_module('qwen_gateway_bootstrap', ROOT / 'scripts/bootstrap_litellm.py')
        initializer = load_module('qwen_gateway_keys', ROOT / 'deploy/litellm/init_keys.py')
        readiness = qwen_local.readiness(args.model_cache, str(args.server))
        require(readiness['status'] == 'ready_for_server_start', 'qwen_preflight_blocked')
        report['host'] = readiness['host']
        cpu_info = Path('/proc/cpuinfo')
        if cpu_info.exists():
            report['host']['cpu_model'] = next((line.split(':', 1)[1].strip()[:160]
                for line in cpu_info.read_text().splitlines() if line.startswith('model name')), 'unknown')
        report['server_authentication'] = 'explicit_api_key_file'
        manifest = qwen_local.deployment_manifest(readiness, args.threads)
        report['deployment_manifest'] = manifest
        report['execution_contract'] = qwen_local.execution_contract(readiness, args.threads)
        report['runtime'] = {'python': sys.version.split()[0], 'httpx': importlib.metadata.version('httpx'),
                             'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        report['source_sha256'] = {
            prefix + '/' + path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for prefix, root, package in (('agentic-system', ROOT, 'agentic'), ('RAGs', args.rags_path, 'rags'))
            for path in sorted((root / package).rglob('*.py'))
        }
        report['source_sha256'].update({
            'agentic-system/' + name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ('scripts/qwen_acceptance.py', 'scripts/qwen_live_worker.py', 'scripts/bootstrap_litellm.py',
                         'deploy/litellm/init_keys.py', 'deploy/litellm/proxy.yaml')
        })
        write_new(args.output / 'deployment.json', manifest)
        with tempfile.TemporaryDirectory(prefix='qwen-live-') as temporary:
            directory = Path(temporary)
            with temporary_gateway_database(admin_url) as database_url, ExitStack() as stack:
                rag_database = stack.enter_context(temporary_gateway_database(admin_url))
                agent_database = stack.enter_context(temporary_gateway_database(admin_url))
                with psycopg.connect(rag_database, autocommit=True, connect_timeout=5) as connection:
                    connection.execute('CREATE EXTENSION vector')
                    report['application_storage'] = {
                        'backend': 'postgresql_pgvector', 'distinct_gateway_rag_agent_databases': True,
                        'postgres_version': connection.execute('SHOW server_version').fetchone()[0],
                        'pgvector_version': connection.execute(
                            "SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0],
                    }
                port, gateway_port = free_port(), free_port()
                while gateway_port == port:
                    gateway_port = free_port()
                endpoint, origin = f'http://127.0.0.1:{port}/v1', f'https://127.0.0.1:{gateway_port}'
                private = directory / 'gateway'
                bootstrap.create(private, endpoint, model='qwen')
                proxy_env = read_env(private / 'proxy.env')
                proxy_env['DATABASE_URL'] = database_url
                upstream_key = proxy_env['QWEN_UPSTREAM_API_KEY']
                rags_key = (private / 'rags/api.key').read_text().strip()
                server_started = time.perf_counter()
                llama = start(stack, processes, directory, 'llama',
                              qwen_local.server_command(str(args.server), args.model_cache, port, args.threads)
                              + ['--api-key-file', str(private / 'upstream.key')], safe_environment())
                with httpx.Client(base_url=endpoint.removesuffix('/v1'), trust_env=False,
                                  headers={'Authorization': 'Bearer ' + upstream_key}) as client:
                    ready(client, llama, '/health')
                    report['model_startup_seconds'] = round(time.perf_counter() - server_started, 6)
                    properties = client.get('/props', timeout=5)
                    require(properties.status_code == 200, 'model_properties_unavailable')
                    props = properties.json()
                    report['server_properties'] = {key: props.get(key) for key in
                                                    ('total_slots', 'model_path', 'default_generation_settings')}
                    # The artifact identity is already recorded; avoid publishing host paths.
                    report['server_properties'].pop('model_path', None)
                    require(props.get('total_slots') == 1, 'unexpected_server_slot_count')
                child_env = safe_environment()
                child_env.update(proxy_env)
                child_env['PATH'] = str(args.gateway_python.parent) + os.pathsep + child_env.get('PATH', '')
                gateway = start(stack, processes, directory, 'gateway', [str(args.gateway_python), '-c',
                    'from litellm_cli.main import proxy_server; proxy_server()', '--config', str(private / 'proxy.yaml'),
                    '--host', '127.0.0.1', '--port', str(gateway_port), '--ssl_keyfile_path', str(private / 'server.key'),
                    '--ssl_certfile_path', str(private / 'server.pem')], child_env)
                context = ssl.create_default_context(cafile=str(private / 'ca.pem'))
                with httpx.Client(base_url=origin, verify=context, trust_env=False) as client:
                    ready(client, gateway, '/health/liveliness')
                    initializer.provision(private, origin, proxy_env['LITELLM_MASTER_KEY'], model_alias=ALIAS)
                    reply = client.get('/v1/models', headers={'Authorization': 'Bearer ' + rags_key})
                    require(reply.status_code == 200 and {m['id'] for m in reply.json()['data']} == {ALIAS},
                            'qwen_gateway_alias_or_scope_invalid')
                clean = safe_environment()
                with environment(clean):
                    # Existing environment() restores caller values; clear proxy and model settings for local I/O.
                    for name in list(os.environ):
                        if 'proxy' in name.lower() or name.startswith(('AGENT_', 'LLAMA_ARG_')):
                            os.environ.pop(name, None)
                    planner_replay(report, directory, endpoint, upstream_key, args.baseline_planner)
                    try:
                        system_workflows(report, stack, processes, directory, args, origin, private,
                                         rag_database, agent_database)
                        report['application_workflows_status'] = 'passed'
                    finally:
                        for name, process in processes:
                            if name == 'worker':
                                stop_process(process)
                        collect_synthetic_trace(report, directory)
                    if not args.smoke_only:
                        reports = {}
                        for label in ('direct', 'gateway'):
                            options = Options.for_preset('qwen-local' if label == 'direct' else 'qwen-litellm',
                                base_url=endpoint if label == 'direct' else origin + '/v1',
                                auth_mode='required', key_env='QWEN_BENCHMARK_KEY',
                                allow_loopback_http=label == 'direct',
                                ca_file='' if label == 'direct' else str(private / 'rags/ca.pem'),
                                deployment_manifest=DeploymentManifest.model_validate(manifest),
                                concurrency=(1, 2, 4), repetitions=args.repetitions, warmup_rounds=1,
                                output_tokens=512, timeout_seconds=90., max_duration_seconds=3600.)
                            print('Live benchmark: ' + label, flush=True)
                            reports[label] = run_benchmark(options, environment={
                                'QWEN_BENCHMARK_KEY': upstream_key if label == 'direct' else rags_key})
                            write_new(args.output / (label + '.json'), reports[label])
                        report['benchmark_status'] = {label: value['status'] for label, value in reports.items()}
                        report['model_memory'] = process_memory(llama.pid)
                        report['gateway_memory'] = process_memory(gateway.pid)
                        if all(value['status'] == 'passed' for value in reports.values()):
                            comparison = compare_reports(reports['direct'], reports['gateway'])
                            require(comparison['deployment_information_complete']
                                    and not comparison['different_deployments']
                                    and not comparison['different_protocols']
                                    and not comparison['different_client_runtime'], 'deployment_parity_failed')
                            comparison['comparison_scope'] = 'same_deployment_gateway_path'
                            write_new(args.output / 'comparison.json', comparison)
                            report['performance_comparison_status'] = 'passed'
                        else:
                            report['performance_comparison_status'] = 'not_generated_due_to_failed_contracts'
                            raise AcceptanceFailure('generation_contract_benchmark_failed')
                report['model_memory'] = process_memory(llama.pid)
                report['gateway_memory'] = process_memory(gateway.pid)
                report['status'] = 'passed'
                report['execution_contract']['model_inference_executed'] = True
                # Stop DB clients before the context drops their disposable database.
                for _, process in reversed(processes):
                    stop_process(process)
    except Exception as exc:
        report['status'] = 'failed'
        report['error_type'] = type(exc).__name__
        report['error_location'] = [{'module': Path(frame.filename).name, 'line': frame.lineno,
                                     'function': frame.name} for frame in traceback.extract_tb(exc.__traceback__)[-5:]]
        if isinstance(exc, AcceptanceFailure):
            report['failure'] = str(exc)
        raise
    finally:
        for _, process in reversed(processes):
            stop_process(process)
        report['elapsed_seconds'] = round(time.perf_counter() - started, 6)
        if report.get('planner_replay') and any(item.get('inference_confirmed') for item in report['planner_replay']):
            report['execution_contract']['model_inference_executed'] = True
        report['child_peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
        write_new(args.output / 'system.json', report)
        print(json.dumps({'status': report['status'], 'elapsed_seconds': report['elapsed_seconds'],
                          'failure': report.get('failure')}), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-cache', type=Path, required=True)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--gateway-python', type=Path, required=True)
    parser.add_argument('--rags-path', type=Path, default=ROOT.parent / 'RAGs')
    parser.add_argument('--rags-python', type=Path, default=ROOT.parent / 'RAGs/.venv/bin/python')
    parser.add_argument('--baseline-planner', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--threads', type=int, default=8)
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--smoke-only', action='store_true')
    args = parser.parse_args()
    try:
        return check(args)
    except Exception as exc:
        # Runtime errors can contain request credentials; retain only a safe type.
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
