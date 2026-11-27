"""실제 외부 MCP 서버(uvx, 네트워크, DART 키)로 등록 표가 맞는지 확인한다. RUN_EXTERNAL_MCP=1일 때만 돈다."""
import os

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.agent.context import repo_settings
from app.agent.external_mcp import EXTERNAL_SERVERS, EXTERNAL_TOOLS, build_external_tools, external_scope
from app.agent.mcp_pool import child_env, open_external_pool, secrets_of

pytestmark = pytest.mark.skipif(os.environ.get("RUN_EXTERNAL_MCP") != "1", reason="RUN_EXTERNAL_MCP=1일 때만")

SAMPLE_ARGS = {
    "dart_largest_shareholder": {"year": 2025},
    "dart_dividend": {"year": 2025},
    "dart_executives": {"year": 2025},
    "dart_financial_index": {"year": 2025, "category": "profitability"},
    "krx_stock_price": {"start_date": "2026-11-20", "end_date": "2026-11-26"},
}


async def _remote_tools(server: str, env: dict) -> dict:
    command = EXTERNAL_SERVERS[server].command()
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    with anyio.fail_after(120):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return {t.name: t for t in (await session.list_tools()).tools}


async def test_registry_matches_remote_schemas():
    settings = repo_settings()
    scope = external_scope("00126380")
    for server in EXTERNAL_SERVERS:
        remote = await _remote_tools(server, child_env(settings, server))
        for tool in (t for t in EXTERNAL_TOOLS if t.server == server):
            assert tool.remote_name in remote, tool.remote_name
            properties = remote[tool.remote_name].input_schema["properties"]
            assert set(tool.to_remote(SAMPLE_ARGS[tool.name], scope)) <= set(properties), tool.name


def test_every_external_tool_answers_for_samsung():
    settings = repo_settings()
    pool = open_external_pool(settings)
    try:
        specs = build_external_tools(pool, external_scope("00126380"), secrets_of(settings))
        results = {s.name: s.run(SAMPLE_ARGS[s.name]) for s in specs}
    finally:
        pool.close()
    for name, result in results.items():
        assert result.ok, (name, result.text[:300])
        assert settings.dart_api_key not in result.text
