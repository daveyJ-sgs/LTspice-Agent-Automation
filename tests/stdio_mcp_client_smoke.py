#!/usr/bin/env python3
"""Launch the real MCP server over stdio and exercise it as a client."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_DIR = Path(__file__).resolve().parents[1]


async def smoke_test() -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "mcp_server.py")],
        cwd=PROJECT_DIR,
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            required = {"list_runs", "list_examples"}
            missing = required - names
            if missing:
                raise RuntimeError(f"MCP tool listing omitted: {sorted(missing)}")

            result = await session.call_tool("list_runs", {"limit": 0})
            if result.is_error:
                raise RuntimeError(f"list_runs failed over stdio: {result.content}")
            if result.structured_content not in (None, {"result": []}):
                raise RuntimeError(
                    "list_runs(limit=0) returned unexpected structured content: "
                    f"{result.structured_content!r}"
                )


if __name__ == "__main__":
    asyncio.run(smoke_test())
    print("MCP stdio client smoke test passed")
