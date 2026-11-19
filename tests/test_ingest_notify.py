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


def test_build_notification():
    from app.ingest.notify import build_notification
    from app.ingest.runner import RunSummary

    assert build_notification(RunSummary(new=3)) is None
    assert build_notification(RunSummary(new=3, failed=2)) == ("공시 수집 완료", "신규 3건, 실패 2건")
    assert build_notification(RunSummary(failed=1, given_up=1)) == ("공시 수집 완료", "신규 0건, 실패 1건, 재시도 중단 1건")
    assert build_notification(RunSummary(aborted_reason="DART 하루 호출 한도 초과")) == ("공시 수집 실패", "DART 하루 호출 한도 초과")
