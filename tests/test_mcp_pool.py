import json
import logging
import re
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
    return ExternalMcpPool({"fake": FAKE}, {"fake": env}, timeout, ["S3CRET"])


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
    pool = ExternalMcpPool({"bad": ["/nonexistent/server"]}, {"bad": {"PATH": os.environ["PATH"]}}, 5, [])
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


def test_stderr_with_secret_is_withheld_even_when_wrapped(caplog):
    caplog.set_level(logging.INFO)
    pool = _pool()
    pool.call("fake", "echo", {})
    pool.close()
    squashed = re.sub(r"\s+", "", caplog.text)
    assert "S3CRET" not in squashed and "S3C" + "RET" not in squashed
    assert "stderr" in caplog.text


def test_stderr_without_secret_is_logged(caplog):
    caplog.set_level(logging.INFO)
    pool = ExternalMcpPool({"fake": FAKE}, {"fake": {"PATH": os.environ["PATH"]}}, 10, ["S3CRET"])
    pool.call("fake", "echo", {})
    pool.close()
    assert "boot" in caplog.text


def test_hang_before_initialize_reports_timeout():
    env = {"PATH": os.environ["PATH"], "FAKE_HANG_BEFORE_INIT": "1"}
    pool = ExternalMcpPool({"fake": FAKE}, {"fake": env}, 1, [])
    try:
        with pytest.raises(ExternalMcpError, match="시간 초과"):
            pool.call("fake", "echo", {})
    finally:
        pool.close()


def test_child_env_has_only_allowed_keys(monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")
    env = child_env(Settings(_env_file=None, dart_api_key="K"), "opendart")
    assert env["OPENDART_API_KEY"] == "K" and "SHOULD_NOT_LEAK" not in env and "PATH" in env


def test_child_env_gives_dart_key_only_to_opendart():
    assert "OPENDART_API_KEY" not in child_env(Settings(_env_file=None, dart_api_key="K"), "pykrx")


def test_krx_credentials_from_dotenv_go_only_to_the_authenticated_server(tmp_path, monkeypatch):
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("KRX_ID=sample-account\nKRX_PW='sample-password'\n")
    settings = Settings(_env_file=env_file)
    assert settings.krx_auth_configured
    assert "sample-account" not in repr(settings) and "sample-password" not in repr(settings)
    for server in ("opendart", "pykrx"):
        assert not {"KRX_ID", "KRX_PW"} & child_env(settings, server).keys()
    env = child_env(settings, "pykrx_authenticated")
    assert env["KRX_ID"] == "sample-account" and env["KRX_PW"] == "sample-password"
    assert "OPENDART_API_KEY" not in env
    assert {"sample-account", "sample-password"} <= set(secrets_of(settings))
    assert not Settings(_env_file=None, krx_id="", krx_pw="sample-password").krx_auth_configured


def test_secrets_of_skips_empty():
    s = Settings(_env_file=None, dart_api_key="D", deepseek_api_key="", tavily_api_key="T", krx_id="", krx_pw="")
    assert secrets_of(s) == ["D", "T"]


def test_open_external_pool_uses_settings(monkeypatch):
    from app.agent import mcp_pool

    captured = {}
    monkeypatch.setattr(mcp_pool, "ExternalMcpPool", lambda *a: captured.setdefault("args", a))
    s = Settings(_env_file=None, dart_api_key="D", krx_id="", krx_pw="")
    assert s.agent_external_mcp is True
    mcp_pool.open_external_pool(s)
    commands, envs, timeout, secrets = captured["args"]
    assert commands["opendart"] == ["uvx", "--with", "mcp<2", "opendart-mcp@0.1.0"]
    assert commands["pykrx"][:7] == ["uvx", "--with", "mcp<2", "--from", "pykrx-mcp@0.1.3", "python", "-c"]
    assert commands["pykrx"] == commands["pykrx_authenticated"]
    assert envs["opendart"]["OPENDART_API_KEY"] == "D" and "OPENDART_API_KEY" not in envs["pykrx"]
    assert timeout == 30 and secrets == ["D"]
