import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from app.agent.external_mcp import ExternalMcpError
from app.agent.mcp_pool import ExternalMcpPool, child_env, secrets_of
from app.config import Settings

FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")]


def _pool(timeout=10):
    env = {"PATH": os.environ["PATH"], "FAKE_SECRET": "S3CRET"}
    return ExternalMcpPool({"fake": FAKE}, env, timeout, ["S3CRET"])


def test_call_reuses_one_process():
    pool = _pool()
    try:
        a = json.loads(pool.call("fake", "echo", {"x": 1})[0])
        b = json.loads(pool.call("fake", "echo", {"x": 2})[0])
    finally:
        pool.close()
    assert a["pid"] == b["pid"] and b["arguments"] == {"x": 2}


def test_child_gets_only_given_env(monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")
    pool = _pool()
    try:
        env = json.loads(pool.call("fake", "echo", {})[0])["env"]
    finally:
        pool.close()
    assert "FAKE_SECRET" in env and "SHOULD_NOT_LEAK" not in env


def test_error_result_is_reported():
    pool = _pool()
    try:
        assert pool.call("fake", "fail", {}) == ("boom", True)
    finally:
        pool.close()


def test_timeout_marks_server_unavailable():
    pool = _pool(timeout=1)
    try:
        started = time.monotonic()
        with pytest.raises(ExternalMcpError):
            pool.call("fake", "sleep", {})
        assert time.monotonic() - started < 6
        started = time.monotonic()
        with pytest.raises(ExternalMcpError):
            pool.call("fake", "echo", {})
        assert time.monotonic() - started < 1
    finally:
        pool.close()


def test_start_failure_raises():
    pool = ExternalMcpPool({"bad": ["/nonexistent/server"]}, {"PATH": os.environ["PATH"]}, 5, [])
    try:
        with pytest.raises(ExternalMcpError):
            pool.call("bad", "echo", {})
    finally:
        pool.close()


def test_unknown_server_raises():
    pool = _pool()
    try:
        with pytest.raises(ExternalMcpError):
            pool.call("nope", "echo", {})
    finally:
        pool.close()


def test_close_is_safe_without_calls_and_twice():
    pool = _pool()
    pool.close()
    pool.close()


def test_stderr_is_logged_redacted(caplog):
    caplog.set_level(logging.INFO)
    pool = _pool()
    pool.call("fake", "echo", {})
    pool.close()
    assert "boot" in caplog.text and "S3CRET" not in caplog.text and "***" in caplog.text


def test_child_env_has_only_allowed_keys(monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")
    env = child_env(Settings(_env_file=None, dart_api_key="K"))
    assert env["OPENDART_API_KEY"] == "K" and "SHOULD_NOT_LEAK" not in env and "PATH" in env


def test_secrets_of_skips_empty():
    s = Settings(_env_file=None, dart_api_key="D", openrouter_api_key="", tavily_api_key="T")
    assert secrets_of(s) == ["D", "T"]


def test_open_external_pool_uses_settings(monkeypatch):
    from app.agent import mcp_pool

    captured = {}
    monkeypatch.setattr(mcp_pool, "ExternalMcpPool", lambda *a: captured.setdefault("args", a))
    s = Settings(_env_file=None, dart_api_key="D")
    assert s.agent_external_mcp is False
    mcp_pool.open_external_pool(s)
    commands, env, timeout, secrets = captured["args"]
    assert commands["opendart"] == ["uvx", "--with", "mcp<2", "opendart-mcp@0.1.0"]
    assert commands["pykrx"] == ["uvx", "--with", "mcp<2", "pykrx-mcp@0.1.3"]
    assert env["OPENDART_API_KEY"] == "D" and timeout == 30 and secrets == ["D"]
