"""로그인된 `claude` CLI에 우리 MCP 서버를 붙여 도구 호출 에이전트로 실행한다.
도구 호출 기록은 MCP 서버가 JSON Lines로 남기고, 여기서 끝난 뒤 읽는다."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.agent.context import REPO_ROOT
from app.agent.tools import TOOL_NAMES
from app.agent.trace import ToolCallRecord, read_records
from app.config import Settings
from app.llm.claude_cli import subscription_env

MCP_SERVER_NAME = "company"

# CLI 내장 도구(웹검색 포함)는 막고 우리 도구만 쓰게 한다.
_DISALLOWED_BUILTINS = [
    "Bash", "Edit", "Write", "Read", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "NotebookEdit",
]


@dataclass(frozen=True)
class ClaudeAgentResult:
    answer: str
    records: list[ToolCallRecord]


def build_mcp_config(corp_code: str, trace_path: Path, max_tool_calls: int) -> dict:
    """비밀값은 넣지 않는다. 서버가 repo_settings()로 .env를 직접 읽는다."""
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": sys.executable,
                "args": ["-m", "app.agent.mcp_server"],
                "env": {
                    "AGENT_CORP_CODE": corp_code,
                    "AGENT_TRACE_PATH": str(trace_path),
                    "AGENT_MAX_TOOL_CALLS": str(max_tool_calls),
                    "PYTHONPATH": str(REPO_ROOT),
                },
            }
        }
    }


def build_claude_command(
    question: str, system_prompt: str, model: str, mcp_config_path: Path
) -> list[str]:
    # 가변 인자 플래그(--allowedTools 등)가 뒤따르는 위치 인자를 삼키지 않도록 질문을 -p 바로 뒤에 둔다.
    return [
        "claude", "-p", question,
        "--system-prompt", system_prompt,
        "--model", model,
        "--output-format", "json",
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools", *[f"mcp__{MCP_SERVER_NAME}__{n}" for n in TOOL_NAMES],
        "--disallowedTools", *_DISALLOWED_BUILTINS,
    ]


def parse_cli_result(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
        answer = payload["result"]
        failed = bool(payload.get("is_error"))
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError("claude CLI 출력을 해석하지 못했습니다") from e
    if failed:
        raise RuntimeError(f"claude CLI 오류: {answer}")
    return str(answer).strip()


def run_claude_agent(
    question: str,
    system_prompt: str,
    corp_code: str,
    settings: Settings,
    runner: Callable = subprocess.run,
) -> ClaudeAgentResult:
    timeout = settings.agent_claude_timeout_seconds
    workdir = Path(tempfile.mkdtemp(prefix="claude-agent-"))
    try:
        trace_path = workdir / "trace.jsonl"
        config_path = workdir / "mcp.json"
        config = build_mcp_config(corp_code, trace_path, settings.agent_max_tool_calls)
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f)

        command = build_claude_command(question, system_prompt, settings.claude_model, config_path)
        try:
            completed = runner(
                command,
                input="",  # stdin을 닫아 두지 않으면 claude가 파이프 입력을 기다린다
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
                env=subscription_env(),
            )
        except FileNotFoundError as e:
            raise RuntimeError("claude CLI 실행 실패 (설치/PATH 확인 필요)") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI 응답이 {timeout}초 안에 오지 않았습니다") from e

        if completed.returncode != 0:
            raise RuntimeError(
                f"claude CLI 오류 (exit {completed.returncode}): {completed.stderr.strip()}"
            )
        return ClaudeAgentResult(parse_cli_result(completed.stdout), read_records(trace_path))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
