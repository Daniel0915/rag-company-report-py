"""macOS 알림 센터 알림 (배치 결과를 사용자에게 알린다)."""

from __future__ import annotations

import logging
import subprocess

from app.ingest.runner import RunSummary

logger = logging.getLogger(__name__)


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
