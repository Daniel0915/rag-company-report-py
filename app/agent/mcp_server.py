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
from app.agent.external_mcp import ToolCaller, build_external_tools, external_scope
from app.agent.mcp_pool import open_external_pool, secrets_of
from app.agent.tools import ToolContext, ToolSpec, build_tools


def mcp_tool_list(specs: list[ToolSpec]) -> list[types.Tool]:
    return [
        types.Tool(name=s.name, description=s.description, input_schema=s.input_schema)
        for s in specs
    ]


def make_call_tool_handler(
    corp_code: str, max_calls: int, trace_path: str | None, settings, pool: ToolCaller | None = None
):
    """call_tool 핸들러. 컨텍스트는 첫 호출에서 만들고, 실패하면 다음 호출에서 다시 시도한다.
    pool이 있으면 외부 MCP 도구도 같은 실행기에 넣는다(이 서버가 외부 서버로 중계한다)."""
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
                specs = build_tools(tool_ctx)
                scope = external_scope(corp_code)
                if pool is not None and scope is not None:
                    specs += build_external_tools(pool, scope, secrets_of(settings))
                executor = ToolExecutor(specs, max_calls, trace_path)
            result = await asyncio.to_thread(executor.execute, params.name, params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(text=result.text)], is_error=not result.ok
        )

    return on_call_tool


def build_server(
    corp_code: str, max_calls: int, trace_path: str | None, settings, pool: ToolCaller | None = None
) -> Server:
    # 목록용 스펙은 연결 없는 컨텍스트로 만든다. 의존성은 도구가 실행될 때만 만져진다.
    listing_specs = build_tools(ToolContext(corp_code, None, None, None, None, None))  # type: ignore[arg-type]
    scope = external_scope(corp_code)
    if pool is not None and scope is not None:
        listing_specs += build_external_tools(None, scope, [])

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=mcp_tool_list(listing_specs))

    return Server(
        "company-report-tools",
        on_list_tools=on_list_tools,
        on_call_tool=make_call_tool_handler(corp_code, max_calls, trace_path, settings, pool),
    )


async def main() -> None:
    settings = repo_settings()
    # 외부 서버는 첫 호출 때 뜨므로 풀을 만들어 두는 비용은 없다.
    pool = open_external_pool(settings) if os.environ.get("AGENT_EXTERNAL_MCP") == "1" else None
    server = build_server(
        os.environ["AGENT_CORP_CODE"],
        int(os.environ.get("AGENT_MAX_TOOL_CALLS", settings.agent_max_tool_calls)),
        os.environ.get("AGENT_TRACE_PATH") or None,
        settings,
        pool,
    )
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if pool is not None:
            await asyncio.to_thread(pool.close)


if __name__ == "__main__":
    asyncio.run(main())
