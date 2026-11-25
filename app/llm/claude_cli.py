"""
로컬에 로그인되어 있는 Claude Code CLI(`claude -p`)를 그대로 호출해서 답변을 받는다.
ANTHROPIC_API_KEY(종량제 API) 대신 claude.ai 구독 계정 로그인 기반으로 동작한다는
프로젝트 방침에 맞춰, API 키 없이 이미 로그인된 CLI 세션을 그대로 쓴다.
`--bare`는 ANTHROPIC_API_KEY를 강제로 요구해서 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class CliCall:
    text: str
    input_tokens: int  # 캐시 생성/읽기 토큰 포함
    output_tokens: int
    cost_usd: float  # CLI가 API 단가로 계산한 값. 구독에서는 실제로 청구되지 않는다


_meter: ContextVar[list[CliCall] | None] = ContextVar("claude_cli_meter", default=None)


@contextmanager
def metered() -> Iterator[list[CliCall]]:
    """이 블록 안에서 일어난 claude CLI 호출을 모은다. 평가가 문항별 토큰과 비용을 남기는 데 쓴다."""
    calls: list[CliCall] = []
    token = _meter.set(calls)
    try:
        yield calls
    finally:
        _meter.reset(token)


def parse_cli_json(stdout: str) -> CliCall:
    """`--output-format json` 출력에서 답과 사용량을 꺼내고, metered() 안이면 기록한다."""
    try:
        payload = json.loads(stdout)
        answer = payload["result"]
        failed = bool(payload.get("is_error"))
        usage = payload.get("usage") or {}
        call = CliCall(
            text=str(answer).strip(),
            input_tokens=sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            ),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError("claude CLI 출력을 해석하지 못했습니다") from e
    if failed:
        raise RuntimeError(f"claude CLI 오류: {answer}")
    calls = _meter.get()
    if calls is not None:
        calls.append(call)
    return call


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
            "--output-format", "json",
            # 도구를 끄고 사용자 설정(전역 CLAUDE.md, 훅, 플러그인)을 읽지 않는다.
            # 안 끄면 호출마다 약 2만 6천 토큰이 프롬프트에 붙는다.
            "--tools", "",
            "--setting-sources", "",
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
        return parse_cli_json(completed.stdout).text
