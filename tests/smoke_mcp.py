"""Real stdio MCP smoke test, against running RAGs and Agentic HTTP services.

Run with two API servers + worker started. Used in CI after the full HTTP flow.
Checks the portable protocol handshake used by Codex and Claude Code clients.
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def probe(module: str, expected: set[str], tool: str, args: dict) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name
            tools = await session.list_tools()
            names = {item.name for item in tools.tools}
            assert expected <= names, (module, expected - names)
            response = await session.call_tool(tool, args)
            assert not response.isError, (module, response)
            assert response.content, (module, "empty tool response")


async def main() -> None:
    await probe(
        "rags.mcp_server",
        {"search_knowledge", "list_knowledge_documents"},
        "search_knowledge",
        {"question": "What is the session timeout?", "mode": "hybrid"},
    )
    await probe(
        "agentic.mcp_server",
        {"start_research_workflow", "get_workflow_status", "get_workflow_events"},
        "start_research_workflow",
        {"goal": "Calculate: 3 * 7"},
    )


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=40))
    print("PASS: real stdio MCP handshakes and tools for both services")
