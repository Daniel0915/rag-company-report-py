import json
import subprocess
from pathlib import Path

import pytest

from app.agent.claude_agent import (
    build_claude_command,
    build_mcp_config,
    run_claude_agent,
)
from app.agent.tools import TOOL_NAMES
from app.config import Settings


def _settings() -> Settings:
    return Settings(_env_file=None)


def _config_from(command: list[str]) -> dict:
    path = command[command.index("--mcp-config") + 1]
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_mcp_config_has_no_secrets(tmp_path):
    cfg = build_mcp_config("00126380", tmp_path / "t.jsonl", 6)
    dumped = json.dumps(cfg)
    assert "tvly-" not in dumped and "TAVILY" not in dumped and "DART_API_KEY" not in dumped
    assert cfg["mcpServers"]["company"]["env"]["AGENT_CORP_CODE"] == "00126380"


def test_claude_command_allows_only_our_tools():
    cmd = build_claude_command("q", "sys", "sonnet", Path("/tmp/c.json"))
    assert cmd[:3] == ["claude", "-p", "q"]
    assert "--strict-mcp-config" in cmd
    # 내장 도구는 전부 끈다(MCP 도구는 별개)
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--disallowedTools" not in cmd
    # 사용자/프로젝트 설정(훅, 플러그인, CLAUDE.md)은 읽지 않는다
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    allowed = cmd[cmd.index("--allowedTools") + 1 :]
    assert allowed[: len(TOOL_NAMES)] == [f"mcp__company__{n}" for n in TOOL_NAMES]


def test_run_claude_agent_reads_trace():
    seen = {}

    def runner(command, **kwargs):
        env = _config_from(command)["mcpServers"]["company"]["env"]
        seen["input"] = kwargs["input"]
        with open(env["AGENT_TRACE_PATH"], "w", encoding="utf-8") as f:
            for seq in (1, 2):
                f.write(json.dumps({
                    "seq": seq, "name": "calculate", "arguments": {"expression": "1+1"},
                    "ok": True, "result_text": "2", "sources": [],
                }) + "\n")
        return subprocess.CompletedProcess(command, 0, '{"result": "답", "is_error": false}', "")

    result = run_claude_agent("q", "sys", "00126380", _settings(), runner=runner)
    assert result.answer == "답"
    assert len(result.records) == 2
    assert seen["input"] == ""


def test_run_claude_agent_cleans_up_temp_files_on_failure():
    created = {}

    def runner(command, **kwargs):
        created["dir"] = Path(kwargs["cwd"])
        assert created["dir"].exists()
        return subprocess.CompletedProcess(command, 1, "", "boom")

    with pytest.raises(RuntimeError, match=r"claude CLI 오류 \(exit 1\): boom"):
        run_claude_agent("q", "sys", "00126380", _settings(), runner=runner)
    assert not created["dir"].exists()


def test_run_claude_agent_cleans_up_on_timeout():
    created = {}

    def runner(command, **kwargs):
        created["dir"] = Path(kwargs["cwd"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(RuntimeError, match="안에 오지 않았습니다"):
        run_claude_agent("q", "sys", "00126380", _settings(), runner=runner)
    assert not created["dir"].exists()


def test_config_file_is_private():
    modes = {}

    def runner(command, **kwargs):
        path = Path(command[command.index("--mcp-config") + 1])
        modes["mode"] = path.stat().st_mode & 0o777
        return subprocess.CompletedProcess(command, 0, '{"result": "x", "is_error": false}', "")

    run_claude_agent("q", "sys", "00126380", _settings(), runner=runner)
    assert modes["mode"] == 0o600


def _set_api_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test")


def test_run_claude_agent_strips_api_credentials(monkeypatch):
    _set_api_env(monkeypatch)
    seen = {}

    def runner(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, '{"result": "x", "is_error": false}', "")

    run_claude_agent("q", "sys", "00126380", _settings(), runner=runner)
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in seen["env"]
    assert "PATH" in seen["env"]


def test_claude_cli_client_strips_api_credentials(monkeypatch):
    from app.llm import claude_cli

    _set_api_env(monkeypatch)
    seen = {}

    def fake_run(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, json.dumps({"result": "답", "is_error": False}), "")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    assert claude_cli.ClaudeCliClient(_settings()).generate("s", "u") == "답"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in seen["env"]
    assert "PATH" in seen["env"]
