import subprocess

from app.ingest.notify import send_notification


def test_send_notification_passes_text_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    send_notification('공시 수집 실패', 'Neo4j "연결" 실패')
    assert calls[0][0] == "osascript" and calls[0][-2:] == ['공시 수집 실패', 'Neo4j "연결" 실패']


def test_send_notification_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    send_notification("t", "m")  # 예외 없음
