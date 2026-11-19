"""macOS 알림 센터 알림 (배치 결과를 사용자에게 알린다)."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from app.ingest.runner import RunSummary

logger = logging.getLogger(__name__)

_DART_KEY = re.compile(r"crtfc_key=[^&\s\"']+")
MAX_ALERT_LINES = 15
MAX_LINE_CHARS = 200
MAX_BODY_CHARS = 2000


def redact(text: str) -> str:
    """DART API 키(`crtfc_key=...`)를 `***`로 가린다. 로그와 알림에 키가 남지 않게 한다."""
    return _DART_KEY.sub("crtfc_key=***", text)


class RedactingFormatter(logging.Formatter):
    """예외 traceback까지 포함한 최종 로그 문장에서 API 키를 가린다."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class ErrorCollector(logging.Handler):
    """이번 실행의 ERROR 메시지를 한 줄씩(traceback 제외, 키 가림) 모아 알림창에 쓴다."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(redact(record.getMessage()))
        except Exception:  # 수집 중 로깅 실패가 수집을 막으면 안 된다
            self.handleError(record)


def send_notification(title: str, message: str) -> None:
    # 텍스트는 스크립트에 끼워 넣지 않고 인자로 넘겨 따옴표 문제를 피한다.
    cmd = [
        "osascript",
        "-e", "on run argv",
        "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e", "end run",
        title,
        message,
    ]
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            logger.warning(
                "macOS 알림 전송 실패(종료 코드 %d): %s", result.returncode, result.stderr.strip()
            )
    except Exception as e:  # 알림 실패가 수집 결과를 바꾸면 안 된다
        logger.warning("macOS 알림 전송 실패: %s", e)


def _alert_body(summary: str, error_lines: list[str]) -> str:
    shown = [
        line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"
        for line in error_lines[:MAX_ALERT_LINES]
    ]
    if len(error_lines) > MAX_ALERT_LINES:
        shown.append(f"외 {len(error_lines) - MAX_ALERT_LINES}건")
    body = summary + "\n\n" + "\n".join(shown)
    return body[:MAX_BODY_CHARS]


def show_failure_alert(title: str, summary: str, error_lines: list[str], log_path: Path) -> None:
    """오류 내용이 담긴 알림창을 띄운다. 기다리지 않고(수집 프로세스는 바로 끝나야 한다) 예외도 삼킨다."""
    # 텍스트는 스크립트에 끼워 넣지 않고 인자로 넘겨 따옴표 문제를 피한다.
    cmd = [
        "osascript",
        "-e", "on run argv",
        "-e", (
            'set r to display alert (item 1 of argv) message (item 2 of argv) as critical '
            'buttons {"닫기", "오류 로그 열기"} default button "오류 로그 열기"'
        ),
        "-e", 'if button returned of r is "오류 로그 열기" then do shell script "open " & quoted form of (item 3 of argv)',
        "-e", "end run",
        title,
        _alert_body(summary, error_lines),
        str(log_path),
    ]
    try:
        subprocess.Popen(
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # 알림 실패가 수집 결과를 바꾸면 안 된다
        logger.warning("실패 알림창 표시 실패: %s", e)


def build_notification(summary: RunSummary) -> tuple[str, str] | None:
    """알릴 만한 결과(실패·일부 실패)일 때만 (제목, 본문)을 돌려준다."""
    if summary.aborted_reason:
        return ("공시 수집 실패", summary.aborted_reason)
    if summary.failed > 0 or summary.given_up > 0:
        message = f"신규 {summary.new}건, 실패 {summary.failed}건"
        if summary.given_up > 0:
            message += f", 재시도 중단 {summary.given_up}건"
        return ("공시 수집 완료", message)
    return None
