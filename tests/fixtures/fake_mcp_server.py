"""테스트용 MCP stdio 서버. echo(인자·pid·환경변수 이름), sleep(10초), fail(is_error) 도구를 낸다."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = [
    types.Tool(name=n, description=n, input_schema={"type": "object", "properties": {}})
    for n in ("echo", "sleep", "fail")
]


async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    if params.name == "sleep":
        await asyncio.sleep(10)
    if params.name == "fail":
        return types.CallToolResult(content=[types.TextContent(text="boom")], is_error=True)
    payload = {"pid": os.getpid(), "arguments": params.arguments or {}, "env": sorted(os.environ)}
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(payload))], is_error=False)


async def main() -> None:
    secret = os.environ.get("FAKE_SECRET", "")
    print("boot", file=sys.stderr, flush=True)
    if secret:
        print("boot crtfc_key=" + secret, file=sys.stderr, flush=True)
        # rich 로그처럼 80칸에서 줄이 꺾여 키가 두 줄로 나뉜 경우
        print(f"GET /api/x.json?a=1&crtfc_k\ney={secret[:3]}\n{secret[3:]}", file=sys.stderr, flush=True)
    if os.environ.get("FAKE_HANG_BEFORE_INIT"):
        await asyncio.sleep(30)
    server = Server("fake", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
