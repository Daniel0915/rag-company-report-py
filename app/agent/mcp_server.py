"""도구 6개를 내보내는 MCP stdio 서버. `claude` CLI가 자식 프로세스로 띄운다."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from app.agent.context import open_tool_context, repo_settings
from app.agent.executor import ToolExecutor
from app.agent.tools import ToolContext, ToolSpec, build_tools


def mcp_tool_list(specs: list[ToolSpec]) -> list[types.Tool]:
    return [
        types.Tool(name=s.name, description=s.description, input_schema=s.input_schema)
        for s in specs
    ]


def make_call_tool_handler(corp_code: str, max_calls: int, trace_path: str | None, settings):
    """call_tool 핸들러. 컨텍스트는 첫 호출에서 만들고, 실패하면 다음 호출에서 다시 시도한다."""
    executor: ToolExecutor | None = None
    lock = asyncio.Lock()

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        nonlocal executor
        async with lock:
            if executor is None:
                try:
                    tool_ctx, _close = open_tool_context(settings, corp_code)
                except Exception as e:  # noqa: BLE001 - 메시지에 비밀이 섞일 수 있어 클래스 이름만 노출
                    return types.CallToolResult(
                        content=[types.TextContent(text=f"도구 준비 실패: {type(e).__name__}")],
                        is_error=True,
                    )
                executor = ToolExecutor(build_tools(tool_ctx), max_calls, trace_path)
            result = await asyncio.to_thread(executor.execute, params.name, params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(text=result.text)], is_error=not result.ok
        )

    return on_call_tool


def build_server(corp_code: str, max_calls: int, trace_path: str | None, settings) -> Server:
    # 목록용 스펙은 연결 없는 컨텍스트로 만든다. 의존성은 도구가 실행될 때만 만져진다.
    listing_specs = build_tools(ToolContext(corp_code, None, None, None, None, None))  # type: ignore[arg-type]

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=mcp_tool_list(listing_specs))

    return Server(
        "company-report-tools",
        on_list_tools=on_list_tools,
        on_call_tool=make_call_tool_handler(corp_code, max_calls, trace_path, settings),
    )


async def main() -> None:
    settings = repo_settings()
    server = build_server(
        os.environ["AGENT_CORP_CODE"],
        int(os.environ.get("AGENT_MAX_TOOL_CALLS", settings.agent_max_tool_calls)),
        os.environ.get("AGENT_TRACE_PATH") or None,
        settings,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
