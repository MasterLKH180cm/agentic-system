"""Compare verified synthetic Store workloads using isolated PostgreSQL schemas.

No model is called. Samples measure store operations, not end-to-end HTTP or LLM
latency. Each revision/repetition/concurrency has its own subprocess and schema.
Only schemas created by this program are removed, after connections are closed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
import uuid


def percentile(values, percentage):
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def measure(operation, samples, concurrency):
    def sample(index):
        start = time.perf_counter()
        operation(index)  # A wrong result fails the complete benchmark.
        return (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        values = list(executor.map(sample, range(samples)))
    elapsed = time.perf_counter() - start
    return {"samples": samples, "validated_results": len(values), "errors": 0,
            "concurrency": concurrency, "elapsed_seconds": elapsed,
            "p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95),
            "p99_ms": percentile(values, 99), "throughput_per_second": samples / elapsed}


@contextmanager
def owned_database(endpoint):
    import psycopg
    from psycopg import sql

    name = "agent_bench_" + uuid.uuid4().hex
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("Benchmark requires an explicit PostgreSQL URL")
    dsn = urlunsplit(parsed._replace(query=urlencode({"options": f"-c search_path={name}"}, quote_via=quote)))
    with psycopg.connect(endpoint, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield dsn
        finally:
            module = sys.modules.get("agentic.pgpool")
            if module is not None:
                module.close_pools(dsn)
            admin.execute("SET lock_timeout = '5s'")
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def run_worker(args):
    sys.path.insert(0, str(args.source_root.resolve()))
    from agentic.schemas import Step, WorkflowIn
    from agentic.store import Store

    source = Path(sys.modules["agentic.store"].__file__).resolve()
    if source.parent.parent != args.source_root.resolve():
        raise ValueError("Imported source differs from selected revision")
    with owned_database(os.environ[args.dsn_env]) as dsn:
        store = Store(dsn, lease_seconds=60)

        def submit():
            return store.submit("benchmark", WorkflowIn(goal="Synthetic six times seven", steps=[
                Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
            ]), max_queued_runs=10000)["id"]

        run = submit()

        def read(_):
            result = store.get("benchmark", run)
            if (result["status"] != "queued" or len(result["steps"]) != 1
                    or result["steps"][0]["args"]["expression"] != "6 * 7"):
                raise AssertionError("Read result differs from expected workflow")

        for index in range(3):
            read(index)
        reads = measure(read, args.samples, args.concurrency[0])
        store.cancel("benchmark", run)

        def execute(_):
            deadline = time.monotonic() + 5
            while (job := store.claim()) is None:
                if time.monotonic() >= deadline:
                    raise AssertionError("Expected runnable workflow was not claimed")
                time.sleep(0.001)
            if job["args"] != {"expression": "6 * 7"} or not store.complete(job, {"value": 42}):
                raise AssertionError("Claim or completion failed validation")

        for index in range(3):
            submit()
            execute(index)
        runs = [submit() for _ in range(args.samples)]
        execution = measure(execute, args.samples, args.concurrency[0])
        for identifier in runs:
            result = store.get("benchmark", identifier)
            if result["status"] != "completed" or result["steps"][0]["result"] != {"value": 42}:
                raise AssertionError("Completed workflow did not preserve its result")
        print(json.dumps({"read": reads, "claim_complete": execution, "warmups_per_workload": 3}))


def source_identity(root):
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256()
    for path in sorted((root / "agentic").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return {"base_commit": commit, "production_source_sha256": digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dsn-env", default="AGENT_TEST_POSTGRES_URL")
    parser.add_argument("--samples", type=int, default=80)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--report", type=Path, default=Path("reports/postgres-comparison-v05.json"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (not 10 <= args.samples <= 1000 or not 1 <= args.repetitions <= 5
            or not args.concurrency or len(args.concurrency) > 4
            or any(not 1 <= count <= 16 for count in args.concurrency)):
        parser.error("Use 10..1000 samples, 1..5 repetitions, and up to four concurrency levels within 1..16")
    if not os.getenv(args.dsn_env):
        parser.error("The selected PostgreSQL environment variable is not set")
    if args.worker:
        run_worker(args)
        return
    if args.baseline_root is None:
        parser.error("A baseline checkout is required for a comparison")
    roots = {"baseline": args.baseline_root.resolve(), "current": args.source_root.resolve()}
    result = {"schema_version": "agentic-postgres-benchmark-v1", "status": "running",
              "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "synthetic direct Store operations on real PostgreSQL; no HTTP/model calls",
              "python": platform.python_version(), "platform": platform.platform(),
              "pool_size_current": os.getenv("AGENT_PG_POOL_SIZE", "4"),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "revisions": {name: source_identity(root) for name, root in roots.items()}, "measurements": []}
    for repeat in range(args.repetitions):
        # Alternate revision order to reduce one-direction warm-cache bias.
        order = list(roots) if repeat % 2 == 0 else list(reversed(roots))
        for count in args.concurrency:
            for name in order:
                command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--source-root", str(roots[name]),
                           "--dsn-env", args.dsn_env, "--samples", str(args.samples), "--concurrency", str(count)]
                process = subprocess.run(command, capture_output=True, text=True, timeout=120)
                if process.returncode:
                    # DSNs and external error bodies are not report diagnostics.
                    raise RuntimeError("Benchmark child failed for " + name + "; inspect the environment separately")
                result["measurements"].append({"revision": name, "repetition": repeat + 1, "concurrency": count,
                                               **json.loads(process.stdout)})
    if any(source_identity(root) != result["revisions"][name] for name, root in roots.items()):
        raise RuntimeError("Source changed during the benchmark; repeat using stable revisions")
    summary = []
    for count in args.concurrency:
        for workload in ("read", "claim_complete"):
            row = {"concurrency": count, "workload": workload}
            for name in roots:
                selected = [item[workload] for item in result["measurements"]
                            if item["revision"] == name and item["concurrency"] == count]
                row[name] = {key: statistics.median(item[key] for item in selected)
                             for key in ("p50_ms", "p95_ms", "p99_ms", "throughput_per_second")}
            row["throughput_ratio"] = row["current"]["throughput_per_second"] / row["baseline"]["throughput_per_second"]
            summary.append(row)
    result.update(status="passed", summary=summary)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "report": str(args.report), "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
