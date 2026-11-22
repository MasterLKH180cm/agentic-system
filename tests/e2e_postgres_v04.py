"""Strict PostgreSQL/pgvector HTTP, restart, feature and shipped-MCP gate."""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

from e2e_support import QUESTION, Services, require, verify_mcp, write_report


EXPECTED = ["native_postgres_identity", "tenant_revision_and_filter_scope", "rag_restart_persistence",
            "independent_worker_and_mcp", "disabled_http_and_mcp", "disabled_agent_submission", "historical_purge"]


def restart(services, process, name):
    require(process.poll() is None, "Cannot restart an already stopped fixture")
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise AssertionError("Fixture needed forced termination during a normal restart")
    replacement = services.start(name, process.args)
    if name.startswith("rag"):
        services.ready(services.rag_url, replacement)
    elif name.startswith("api"):
        services.ready(services.agent_url, replacement)
    return replacement


async def disabled_mcp(services):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    # Keep local bridge permissions enabled so the real HTTP server determines listing.
    env = {**services.env, "RAGS_FEATURES_JSON": "{}"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "rags.mcp_server"], env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            require("search_knowledge" not in names and "list_knowledge_documents" in names,
                    "Shipped MCP bridge advertised a disabled HTTP operation")
            rejected = await session.call_tool("search_knowledge", {"question": QUESTION})
            require(rejected.isError, "Unadvertised disabled MCP query executed")
            listing = await session.call_tool("list_knowledge_documents", {})
            require(not listing.isError, "Enabled MCP document listing stopped working")


def run(services, checks):
    import psycopg
    require(services.rag_storage["backend"] == "postgresql" and
            services.rag_storage["vector_search"] == "pgvector_exact" and
            services.agent_storage["backend"] == "postgresql", "Selected PostgreSQL services fell back")
    with psycopg.connect(services.rag_postgres) as connection:
        require(connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0] == "0.8.7",
                "Wrong pgvector extension")
        require(connection.execute("SELECT pg_typeof(embedding)::text FROM chunks WHERE tenant=%s LIMIT 1",
                                   (services.tenant,)).fetchone()[0] == "vector", "Embeddings are not native vectors")
    checks.append({"id": EXPECTED[0], "passed": True, "vector_extension": "0.8.7", "native_type": "vector"})
    document = {"document_id": "retention-v04", "title": "Retention policy",
                "metadata": {"verification": "pg-v04"}, "source": "operator-policy"}
    first = services.request("POST", "/documents", rag=True, expected=201, data={
        **document, "text": "Retention period is 10 days.", "valid_from": "2024-01-01T00:00:00Z"})
    second = services.request("POST", "/documents", rag=True, expected=201, data={
        **document, "text": "Retention period is 20 days.", "valid_from": "2024-06-01T00:00:00Z",
        "expected_revision": first["revision"]})
    services.request("POST", "/documents", rag=True, token=services.other_rag_key, expected=201, data={
        **document, "text": "Retention period is 999 days."})
    services.request("POST", "/documents", rag=True, expected=409, data={
        **document, "text": "Retention period is 777 days.", "expected_revision": first["revision"]})
    query = {"question": "What is the retention period?", "mode": "dense", "generate": False,
             "filters": {"metadata": {"verification": "pg-v04"}}}
    current = services.request("POST", "/query", rag=True, data=query)
    historical = services.request("POST", "/query", rag=True,
                                  data={**query, "as_of": "2024-03-01T00:00:00Z"})
    require(current["citations"] and all("20 days" in item["text"] for item in current["citations"]),
            "Dense query leaked a tenant, stale revision or failed CAS")
    require(historical["citations"] and all("10 days" in item["text"] for item in historical["citations"]),
            "Historical dense query did not respect revision validity")
    require(len(services.request("GET", "/documents/retention-v04/revisions", rag=True)) == 2,
            "Revision history changed across tenants or a failed mutation")
    checks.append({"id": EXPECTED[1], "passed": True, "authorized_revisions": 2, "foreign_citations": 0})

    rag_process = next(process for name, process, _log in services.children if name == "rag")
    rag_process = restart(services, rag_process, "rag-restarted")
    require(services.request("GET", "/documents/retention-v04", rag=True)["revision"] == second["revision"],
            "Source revision was lost on restart")
    require(services.request("POST", "/query", rag=True, data=query)["citations"] == current["citations"],
            "Persistent vector retrieval changed on restart")
    checks.append({"id": EXPECTED[2], "passed": True, "same_revision_and_citations": True})

    run_id = services.submit(QUESTION)
    state = services.finish(run_id)
    require(state["status"] == "completed" and state["steps"][0]["result"]["citations"],
            "Real independent worker failed to consume PostgreSQL-backed evidence")
    verify_mcp(services, run_id, "completed")
    checks.append({"id": EXPECTED[3], "passed": True, "terminal_state": "completed"})

    before = {item["id"] for item in services.request("GET", "/documents", rag=True)}
    services.env["RAGS_FEATURES_JSON"] = '{"ingestion":false,"query":false}'
    rag_process = restart(services, rag_process, "rag-flags-disabled")
    capabilities = services.request("GET", "/capabilities", rag=True)
    require(capabilities["features"]["ingestion"] is False and capabilities["features"]["query"] is False,
            "Effective RAG flags were not reported")
    for path, body in (("/documents", {"document_id": "forbidden-v04", "title": "Blocked", "text": "Do not persist"}),
                       ("/query", query)):
        response = services.request("POST", path, rag=True, expected=403, data=body)
        require(response["detail"]["code"] == "feature_disabled", "Disabled HTTP operation did not fail explicitly")
    require({item["id"] for item in services.request("GET", "/documents", rag=True)} == before,
            "Disabled ingestion mutated durable source state")
    asyncio.run(asyncio.wait_for(disabled_mcp(services), timeout=25))
    checks.append({"id": EXPECTED[4], "passed": True, "mutation_rejected": True,
                   "query_rejected": True, "shipped_mcp_listing_and_rejection": True})

    services.env["AGENT_FEATURES_JSON"] = '{"math":false}'
    api_process = next(process for name, process, _log in services.children if name == "api")
    restart(services, api_process, "api-flags-disabled")
    services.worker = restart(services, services.worker, "worker-flags-disabled")
    require(services.request("GET", "/capabilities")["features"]["math"] is False,
            "Effective Agent flag was not reported")
    before_runs = {item["id"] for item in services.request("GET", "/workflows")}
    response = services.request("POST", "/workflows", expected=403, data={
        "goal": "Calculate 2 + 2", "steps": [{"id": "calculate", "tool": "math.calculate",
                                            "args": {"expression": "2 + 2"}}]})
    require(response["detail"]["code"] == "feature_disabled", "Disabled Agent tool was admitted")
    require({item["id"] for item in services.request("GET", "/workflows")} == before_runs,
            "Disabled Agent submission created a durable run")
    checks.append({"id": EXPECTED[5], "passed": True, "no_new_workflow": True})

    services.env["RAGS_FEATURES_JSON"] = "{}"
    restart(services, rag_process, "rag-flags-restored")
    services.request("DELETE", "/documents/retention-v04", rag=True)
    require(not services.request("POST", "/query", rag=True,
                                 data={**query, "as_of": "2024-03-01T00:00:00Z"})["citations"],
            "Deleted source remained visible through historical retrieval")
    require(services.request("GET", "/documents/retention-v04", rag=True,
                             token=services.other_rag_key)["id"] == "retention-v04",
            "Deletion crossed a tenant boundary")
    checks.append({"id": EXPECTED[6], "passed": True, "history_purged": True, "other_tenant_preserved": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    services = Services(step_timeout=45, postgres=os.getenv("AGENT_E2E_POSTGRES_URL"),
                        rag_postgres=os.getenv("RAGS_E2E_POSTGRES_URL"))
    report = {"status": "failed", "schema_version": "pgvector-http-v04", "expected_checks": EXPECTED,
              "checks": [], "live_paid_provider": False, "database_state_modified_by_test": False}
    try:
        require(services.postgres and services.rag_postgres, "Both disposable PostgreSQL URLs are required")
        with services:
            report["storage"] = {"rag": services.rag_storage, "agent": services.agent_storage}
            run(services, report["checks"])
        require([check["id"] for check in report["checks"]] == EXPECTED, "A required PostgreSQL check did not execute")
        report["status"] = "passed"
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)[:1000]}
        report["diagnostics"] = services.diagnostics
        raise
    finally:
        report["cleanup_completed"] = services.cleaned
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        write_report(args.report, report)
        print(json.dumps({"status": report["status"], "completed_checks": len(report["checks"]),
                          "expected_checks": len(EXPECTED)}))


if __name__ == "__main__":
    main()
