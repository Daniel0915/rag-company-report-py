import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolRequestParams

import app.agent.context as context
import app.agent.mcp_server as mcp_server
from app.agent.context import REPO_ROOT, repo_settings
from app.agent.mcp_server import mcp_tool_list
from app.agent.tools import TOOL_NAMES, build_tools
from app.config import Settings
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


@pytest.mark.parametrize("krx_pw", ["", "sample-password"])
async def test_mcp_server_lists_external_tools_over_stdio_when_enabled(tmp_path, krx_pw):
    from app.agent.external_mcp import available_external_tools

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.agent.mcp_server"],
        env={
            "AGENT_CORP_CODE": "00126380",
            "AGENT_EXTERNAL_MCP": "1",
            "PYTHONPATH": str(REPO_ROOT),
            "KRX_ID": "sample-account",
            "KRX_PW": krx_pw,
        },
        cwd=tmp_path,
    )
    with anyio.fail_after(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    assert [t.name for t in result.tools] == list(TOOL_NAMES) + [
        tool.name for tool in available_external_tools(bool(krx_pw))
    ]


async def test_call_tool_routes_external_tools_to_pool(monkeypatch):
    monkeypatch.setattr(mcp_server, "open_tool_context", lambda s, c: (fake_ctx(), lambda: None))
    monkeypatch.setattr(mcp_server, "secrets_of", lambda s: [])

    class Pool:
        def call(self, server, tool, arguments):
            return '{"status":"000","list":[{"rcept_no":"1","thstrm":"1,446"}]}', False

    handler = mcp_server.make_call_tool_handler("00126380", 6, None, Settings(_env_file=None), Pool())
    result = await _call(handler, "dart_dividend", {"year": 2025})
    assert not result.is_error and "1,446" in result.content[0].text


def _call(handler, name, arguments):
    return handler(None, CallToolRequestParams(name=name, arguments=arguments))


async def test_call_tool_runs_tool_through_executor(monkeypatch):
    monkeypatch.setattr(mcp_server, "open_tool_context", lambda s, c: (fake_ctx(), lambda: None))
    handler = mcp_server.make_call_tool_handler("00126380", 6, None, object())
    result = await _call(handler, "calculate", {"expression": "1+2*3"})
    assert result.content[0].text == "7"
    assert not result.is_error


async def test_call_tool_context_failure_exposes_only_class_name_and_retries(monkeypatch):
    attempts = []

    def failing_open(settings, corp_code):
        attempts.append(1)
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(mcp_server, "open_tool_context", failing_open)
    handler = mcp_server.make_call_tool_handler("00126380", 6, None, object())
    result = await _call(handler, "calculate", {"expression": "1+1"})
    assert result.is_error
    assert "RuntimeError" in result.content[0].text
    assert "hunter2" not in result.content[0].text
    await _call(handler, "calculate", {"expression": "1+1"})
    assert len(attempts) == 2


def test_open_tool_context_closes_partial_resources_on_failure(monkeypatch):
    closed = []

    class Closable:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    monkeypatch.setattr(context.GraphDatabase, "driver", lambda *a, **k: Closable("driver"))
    monkeypatch.setattr(context, "OllamaClient", lambda s: Closable("ollama"))

    def boom(settings):
        raise ValueError("secret")

    monkeypatch.setattr(context, "DartApiClient", boom)
    with pytest.raises(ValueError):
        context.open_tool_context(context.Settings(_env_file=None), "00126380")
    assert closed == ["ollama", "driver"]
