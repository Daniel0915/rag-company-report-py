import subprocess

import logging
from pathlib import Path

from app.ingest.notify import ErrorCollector, redact, send_notification, show_failure_alert


def test_send_notification_passes_text_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
    )
    send_notification('공시 수집 실패', 'Neo4j "연결" 실패')
    assert calls[0][0] == "osascript" and calls[0][-2:] == ['공시 수집 실패', 'Neo4j "연결" 실패']


def test_send_notification_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    send_notification("t", "m")  # 예외 없음


def test_send_notification_logs_warning_on_nonzero_exit(monkeypatch, caplog):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "not allowed")
    )
    with caplog.at_level("WARNING"):
        send_notification("t", "m")  # 예외 없음
    assert "not allowed" in caplog.text and "1" in caplog.text


def test_build_notification():
    from app.ingest.notify import build_notification
    from app.ingest.runner import RunSummary

    assert build_notification(RunSummary(new=3)) is None
    assert build_notification(RunSummary(new=3, failed=2)) == ("공시 수집 완료", "신규 3건, 실패 2건")
    assert build_notification(RunSummary(failed=1, given_up=1)) == ("공시 수집 완료", "신규 0건, 실패 1건, 재시도 중단 1건")
    assert build_notification(RunSummary(aborted_reason="DART 하루 호출 한도 초과")) == ("공시 수집 실패", "DART 하루 호출 한도 초과")


def test_redact_hides_dart_key():
    out = redact("GET https://x/list.json?crtfc_key=abc123&corp_code=1")
    assert "abc123" not in out and "crtfc_key=***&corp_code=1" in out


def test_error_collector_keeps_error_messages_only():
    collector = ErrorCollector()
    log = logging.getLogger("test.collector")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    log.addHandler(collector)
    try:
        log.info("info line")
        log.warning("warn line")
        log.error("err crtfc_key=SECRET line")
    finally:
        log.removeHandler(collector)
    assert collector.lines == ["err crtfc_key=*** line"]


def _capture_popen(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))

    monkeypatch.setattr(subprocess, "Popen", fake)
    return calls


def test_show_failure_alert_passes_text_as_arguments(monkeypatch):
    calls = _capture_popen(monkeypatch)
    show_failure_alert("제목", "요약", ["R1: boom"], Path("/tmp/x.errors.log"))
    cmd, kw = calls[0]
    assert cmd[0] == "osascript"
    assert cmd[-3] == "제목" and "요약" in cmd[-2] and "R1: boom" in cmd[-2]
    assert cmd[-1] == "/tmp/x.errors.log"
    assert kw["start_new_session"] is True


def test_show_failure_alert_truncates(monkeypatch):
    calls = _capture_popen(monkeypatch)
    lines = ["x" * 300] + [f"line{i}" for i in range(19)]
    show_failure_alert("t", "s", lines, Path("/tmp/l"))
    body = calls[0][0][-2]
    assert "x" * 200 + "…" in body and "x" * 201 not in body
    assert "line13" in body and "line14" not in body
    assert "외 5건" in body


def test_show_failure_alert_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "Popen", boom)
    show_failure_alert("t", "s", ["e"], Path("/tmp/l"))  # 예외 없음
