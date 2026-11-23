"""Bounded diagnostics for check_qwen_live's isolated, synthetic-only worker.

Never use this launcher with user data: it deliberately preserves generated text.
It records no request text, HTTP headers, URLs, credentials, or exception messages.
Production workers continue to use ``python -m agentic.worker`` without capture.
"""
import argparse
import hashlib
import json
import os
import sys
import traceback
from contextvars import ContextVar
from pathlib import Path

import httpx


def response_evidence(payload):
    """Copy only bounded completion fields, never arbitrary provider metadata."""
    result = {}
    choices = payload.get('choices') if isinstance(payload, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        message = choice.get('message')
        if isinstance(message, dict):
            for field in ('content', 'refusal'):
                value = message.get(field)
                if value is None:
                    result[field] = None
                elif isinstance(value, str):
                    encoded = value.encode('utf-8')
                    result[field] = encoded[:16384].decode('utf-8', errors='ignore')
                    result[field + '_truncated'] = len(encoded) > 16384
        finish = choice.get('finish_reason')
        if isinstance(finish, str):
            result['finish_reason'] = finish[:80]
    usage = payload.get('usage') if isinstance(payload, dict) else None
    if isinstance(usage, dict):
        result['usage'] = {key: value for key, value in usage.items()
                           if key in {'prompt_tokens', 'completion_tokens', 'total_tokens'}
                           and type(value) is int and 0 <= value <= 10**9}
    return result


def exception_evidence(exc):
    result = {'error_type': type(exc).__name__, 'error_location': [
        {'module': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
        for frame in traceback.extract_tb(exc.__traceback__)[-6:]]}
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(exc.response, 'status_code', None)
        if type(status) is int and 100 <= status <= 599:
            result['http_status'] = status
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--synthetic-trace', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('AGENT_ENV') != 'test':
        parser.error('Synthetic trace requires the disposable test environment')
    identities = json.loads(os.environ.get('AGENT_KEYS_JSON', '{}'))
    if not identities or set(identities.values()) != {'qwen-live-synthetic'}:
        parser.error('Synthetic trace requires the dedicated synthetic tenant')
    from agentic.model import ModelGateway
    from agentic.tools import Tools
    from agentic.worker import main as worker_main

    active = ContextVar('qwen_synthetic_call', default=None)
    original_request, original_complete = ModelGateway._request, ModelGateway.complete
    original_execute = Tools.execute
    written = 0
    failures = 0
    output = args.synthetic_trace.open('x', encoding='utf-8')
    os.chmod(args.synthetic_trace, 0o600)

    def capture_failed():
        nonlocal failures
        failures += 1
        try:
            print('QWEN_CAPTURE_FAILURE count=' + str(failures), file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001 - diagnostics cannot affect application results.
            # Even an unavailable diagnostic stream cannot change a tool result.
            return

    def record(build_item):
        nonlocal written
        if written >= 128:
            return
        try:
            item = {**build_item(), 'capture_failures': failures}
            line = json.dumps(item, ensure_ascii=False, allow_nan=False) + '\n'
            # Count attempts: a failed flush may already have written a record.
            written += 1
            output.write(line)
            output.flush()
        except Exception:  # noqa: BLE001 - capture is intentionally best-effort.
            capture_failed()

    async def request(*request_args, **request_kwargs):
        reply = await original_request(*request_args, **request_kwargs)
        record(lambda: {'event': 'model_response', **(active.get() or {}), 'http_status': reply.status_code,
                        **response_evidence(reply.json())})
        return reply

    async def complete(self, system, message, **kwargs):
        context = {}
        try:
            schema = kwargs.get('response_schema')
            context = {'run_id': kwargs.get('run_id'), 'purpose': kwargs.get('purpose', 'synthesis'),
                       'prompt_sha256': hashlib.sha256((system + '\n' + message).encode()).hexdigest(),
                       'schema_sha256': None if schema is None else hashlib.sha256(json.dumps(
                           schema, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()}
        except Exception:  # noqa: BLE001 - preserve the original model validation path.
            capture_failed()
        token = active.set(context)
        try:
            return await original_complete(self, system, message, **kwargs)
        except Exception as exc:
            record(lambda error=exc: {'event': 'model_failure', **context, **exception_evidence(error)})
            raise
        finally:
            active.reset(token)

    async def execute(self, job, inputs):
        try:
            return await original_execute(self, job, inputs)
        except Exception as exc:
            record(lambda error=exc: {'event': 'tool_failure', 'run_id': job.get('run_id'),
                                     'tool': job.get('tool'), **exception_evidence(error)})
            raise

    try:
        ModelGateway._request = staticmethod(request)
        ModelGateway.complete = complete
        Tools.execute = execute
        worker_main()
    finally:
        ModelGateway._request = staticmethod(original_request)
        ModelGateway.complete = original_complete
        Tools.execute = original_execute
        try:
            output.close()
        except Exception:  # noqa: BLE001 - close must not hide a worker exception.
            capture_failed()


if __name__ == '__main__':
    main()
