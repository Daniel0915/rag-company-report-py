import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import app.agent.context as context
from app.agent.context import REPO_ROOT, repo_settings
from app.agent.mcp_server import mcp_tool_list
from app.agent.tools import TOOL_NAMES, build_tools
from test_agent_tools import fake_ctx


def test_mcp_tool_list_matches_specs():
    specs = build_tools(fake_ctx())
    tools = mcp_tool_list(specs)
    assert [t.name for t in tools] == list(TOOL_NAMES)
    assert tools[0].input_schema == specs[0].input_schema
    assert tools[0].description == specs[0].description


def test_repo_settings_reads_repo_env_regardless_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    captured = {}

    class FakeSettings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(context, "Settings", FakeSettings)
    repo_settings()
    assert captured["_env_file"] == REPO_ROOT / ".env"
    assert captured["_env_file"].is_absolute()


async def test_mcp_server_lists_tools_over_stdio_without_external_services(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.agent.mcp_server"],
        env={"AGENT_CORP_CODE": "00126380", "PYTHONPATH": str(REPO_ROOT)},
        cwd=tmp_path,
    )
    with anyio.fail_after(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    assert [t.name for t in result.tools] == list(TOOL_NAMES)
    ownership = next(t for t in result.tools if t.name == "query_ownership_graph")
    assert "00126380" in ownership.description
