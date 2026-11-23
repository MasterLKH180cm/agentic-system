"""Operator wrapper retaining progress/resources for the unchanged frozen replay."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import subprocess
import sys
import time

ROOT = Path('/workspace/scratch/7113f13a25c9')
RAGS = ROOT / 'RAGs'
REPORT = ROOT / 'agentic-system/reports/qwen-admission-replay-20261123.json'
RESOURCES = REPORT.with_name(REPORT.stem + '-resources.json')
LOG = ROOT / 'qwen-admission-replay-20261123-progress.log'
TIMEOUT = 3600
for path in (REPORT, RESOURCES, LOG):
    if path.exists():
        raise SystemExit('Refusing to overwrite an existing replay artifact')
command = [str(ROOT / 'qwen-admission-venv/bin/python'), '-u', '-m', 'rags.verify_admission',
           '--profile', 'profiles/admission-qwen-v03.json', '--cache-dir', str(ROOT / 'qwen-model-cache'),
           '--report', str(REPORT), '--enforce-quality']
env = {key: value for key, value in os.environ.items() if 'proxy' not in key.lower()}
env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
before = resource.getrusage(resource.RUSAGE_CHILDREN)
started = time.perf_counter()
record = {
    'schema_version': 'frozen-qwen-replay-resources-v1',
    'started_at': datetime.now(timezone.utc).isoformat(),
    'timeout_seconds': TIMEOUT,
    'module': 'rags.verify_admission',
    'arguments': {'profile': 'profiles/admission-qwen-v03.json', 'enforce_quality': True,
                  'holdout': 'examples/admission_holdout_v03.json', 'regression': 'examples/eval_v02.json'},
    'source_sha256': {name: hashlib.sha256((RAGS / name).read_bytes()).hexdigest() for name in (
        'rags/verify_admission.py', 'rags/evaluate.py', 'rags/admission_eval.py',
        'profiles/admission-qwen-v03.json', 'examples/admission_holdout_v03.json', 'examples/eval_v02.json')},
    'wrapper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'host': {'logical_cpus': os.cpu_count(),
             'cpu_model': next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                                if line.startswith('model name')), 'unknown'),
             'memory_total_bytes': int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                                            if line.startswith('MemTotal:'))) * 1024},
    'network_mode': 'offline_cache_only',
    'other_model_inference_scheduled': False,
    'quality_gate_modified': False,
}
process = None
try:
    process = subprocess.Popen(command, cwd=RAGS, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    with LOG.open('xb') as log:
        while selector.get_map():
            if time.perf_counter() - started > TIMEOUT:
                record['timeout'] = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
                break
            for key, _ in selector.select(timeout=1):
                chunk = key.fileobj.read1(16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                log.write(chunk)
                log.flush()
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        selector.close()
    record['process_exit_code'] = process.wait(timeout=10)
except Exception as exc:
    record['wrapper_error_type'] = type(exc).__name__
    raise
finally:
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    record.update(
        finished_at=datetime.now(timezone.utc).isoformat(),
        wall_seconds=round(time.perf_counter() - started, 6),
        child_peak_rss_bytes=after.ru_maxrss * 1024,
        child_user_cpu_seconds=round(after.ru_utime - before.ru_utime, 6),
        child_system_cpu_seconds=round(after.ru_stime - before.ru_stime, 6),
        complete_report_written=REPORT.exists(),
        progress_log_sha256=hashlib.sha256(LOG.read_bytes()).hexdigest() if LOG.exists() else None,
    )
    if REPORT.exists():
        measured = json.loads(REPORT.read_text())
        record.update(report_sha256=hashlib.sha256(REPORT.read_bytes()).hexdigest(),
                      report_exit_status=measured['exit_status'],
                      correctness_passed=measured['correctness']['passed'],
                      quality_passed=measured['quality']['passed'],
                      promotion_status=measured['promotion_status'])
    with RESOURCES.open('x') as output:
        json.dump(record, output, indent=2, allow_nan=False)
        output.write('\n')
    print(json.dumps(record, indent=2), flush=True)
raise SystemExit(record.get('process_exit_code', 1))
