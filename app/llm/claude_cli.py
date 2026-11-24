"""
로컬에 로그인되어 있는 Claude Code CLI(`claude -p`)를 그대로 호출해서 답변을 받는다.
ANTHROPIC_API_KEY(종량제 API) 대신 claude.ai 구독 계정 로그인 기반으로 동작한다는
프로젝트 방침에 맞춰, API 키 없이 이미 로그인된 CLI 세션을 그대로 쓴다.
`--bare`는 ANTHROPIC_API_KEY를 강제로 요구해서 쓰지 않는다.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from app.config import Settings


def subscription_env() -> dict[str, str]:
    """API 키 계열 변수를 뺀 환경. 있으면 -p 모드가 종량제 API 과금으로 바뀌므로 구독 로그인만 쓰게 한다."""
    return {
        k: v for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }


class ClaudeCliClient:
    def __init__(self, settings: Settings) -> None:
        self._model = settings.claude_model
        self._timeout = settings.claude_timeout_seconds

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        command = [
            "claude", "-p", user_prompt,
            "--system-prompt", system_prompt,
            "--model", self._model,
            "--output-format", "text",
            "--allowedTools", "",
        ]
        try:
            # stdin을 안 닫아두면 claude가 파이프 입력을 기다리다 경고를 찍는다.
            # 프롬프트를 인자로만 주니 빈 stdin으로 즉시 EOF를 준다.
            completed = subprocess.run(
                command,
                input="",
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=tempfile.gettempdir(),
                env=subscription_env(),
            )
        except FileNotFoundError as e:
            raise RuntimeError("claude CLI 실행 실패 (설치/PATH 확인 필요)") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI 응답이 {self._timeout}초 안에 오지 않았습니다") from e

        if completed.returncode != 0:
            raise RuntimeError(
                f"claude CLI 오류 (exit {completed.returncode}): {completed.stderr.strip()}"
            )
        return completed.stdout.strip()
