"""요청 하나 동안 쓰는 외부 MCP 세션 풀.

도구 실행(ToolSpec.run)은 동기 함수이고 MCP 클라이언트는 비동기라, anyio 블로킹 포털(별도 스레드의
이벤트 루프)에서 세션을 연다. 서버는 그 서버의 도구가 처음 불릴 때 띄우고 close()에서 닫는다.
"""
from __future__ import annotations

import logging
import os
import tempfile
from contextlib import AbstractContextManager, asynccontextmanager
from typing import IO, AsyncIterator, Mapping, Sequence

import anyio
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.agent.external_mcp import EXTERNAL_SERVERS, ExternalMcpError, redact_secrets
from app.config import Settings

logger = logging.getLogger(__name__)

STDERR_TAIL_LINES = 20
# uvx가 패키지 캐시와 실행 파일을 찾는 데 필요한 것만 자식에게 넘긴다.
_PASSTHROUGH_ENV = ("PATH", "HOME", "UV_CACHE_DIR")


def child_env(settings: Settings) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if os.environ.get(k)}
    for server in EXTERNAL_SERVERS.values():
        for env_name, attr in server.secret_env.items():
            env[env_name] = getattr(settings, attr)
    return env


def secrets_of(settings: Settings) -> list[str]:
    values = (settings.dart_api_key, settings.deepseek_api_key, settings.tavily_api_key)
    return [v for v in values if v]


@asynccontextmanager
async def _open_session(
    params: StdioServerParameters, errlog: IO[str], timeout: float
) -> AsyncIterator[ClientSession]:
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            with anyio.fail_after(timeout):
                await session.initialize()
            yield session


async def _call_tool(session: ClientSession, tool: str, arguments: dict, timeout: float):
    with anyio.fail_after(timeout):
        return await session.call_tool(tool, arguments)


class ExternalMcpPool:
    def __init__(
        self,
        commands: Mapping[str, list[str]],
        env: Mapping[str, str],
        timeout_seconds: float,
        secrets: Sequence[str],
    ) -> None:
        self._commands = dict(commands)
        self._env = dict(env)
        self._timeout = timeout_seconds
        self._secrets = list(secrets)
        self._portal_cm: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._sessions: dict[str, tuple[AbstractContextManager[ClientSession], ClientSession]] = {}
        self._errlogs: dict[str, IO[str]] = {}
        self._broken: set[str] = set()
        self._closed = False

    def _ensure_portal(self) -> BlockingPortal:
        if self._portal is None:
            self._portal_cm = start_blocking_portal()
            self._portal = self._portal_cm.__enter__()
        return self._portal

    def _session(self, server: str) -> ClientSession:
        if server in self._sessions:
            return self._sessions[server][1]
        command = self._commands[server]
        errlog = self._errlogs[server] = tempfile.TemporaryFile("w+", encoding="utf-8")
        params = StdioServerParameters(command=command[0], args=command[1:], env=self._env)
        portal = self._ensure_portal()
        cm = portal.wrap_async_context_manager(_open_session(params, errlog, self._timeout))
        session = cm.__enter__()
        self._sessions[server] = (cm, session)
        return session

    def call(self, server: str, tool: str, arguments: dict) -> tuple[str, bool]:
        if self._closed:
            raise ExternalMcpError("풀이 이미 닫혔습니다")
        if server not in self._commands:
            raise ExternalMcpError(f"등록되지 않은 외부 서버: {server}")
        if server in self._broken:
            raise ExternalMcpError(f"{server} 서버를 이번 요청에서 쓸 수 없습니다")
        try:
            session = self._session(server)
            result = self._ensure_portal().call(_call_tool, session, tool, arguments, self._timeout)
        except Exception as e:  # noqa: BLE001 - 시작 실패, 시간 초과, 연결 끊김을 한 가지 오류로 모은다
            self._broken.add(server)
            reason = "시간 초과" if isinstance(e, TimeoutError) else f"{type(e).__name__}: {e}"
            raise ExternalMcpError(redact_secrets(f"{server} 서버 오류 ({reason})", self._secrets)) from e
        text = "".join(getattr(c, "text", "") for c in result.content)
        return text, bool(result.is_error)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for server, (cm, _session) in self._sessions.items():
            try:
                cm.__exit__(None, None, None)
            except Exception as e:  # noqa: BLE001 - 닫기 실패는 기록만 하고 나머지를 계속 닫는다
                logger.info("외부 MCP 서버 %s 닫기 실패: %s", server, type(e).__name__)
        self._sessions.clear()
        if self._portal_cm is not None:
            self._portal_cm.__exit__(None, None, None)
        for server, errlog in self._errlogs.items():
            errlog.seek(0)
            tail = errlog.read().splitlines()[-STDERR_TAIL_LINES:]
            errlog.close()
            if tail:
                logger.info(
                    "외부 MCP 서버 %s stderr:\n%s", server, redact_secrets("\n".join(tail), self._secrets)
                )


def open_external_pool(settings: Settings) -> ExternalMcpPool:
    return ExternalMcpPool(
        {name: server.command() for name, server in EXTERNAL_SERVERS.items()},
        child_env(settings),
        settings.agent_external_mcp_timeout_seconds,
        secrets_of(settings),
    )
