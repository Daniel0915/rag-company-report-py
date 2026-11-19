"""macOS 알림 센터 알림 (배치 결과를 사용자에게 알린다)."""

from __future__ import annotations

import logging
import subprocess

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
        subprocess.run(cmd, check=False, capture_output=True, timeout=10)
    except Exception as e:  # 알림 실패가 수집 결과를 바꾸면 안 된다
        logger.warning("macOS 알림 전송 실패: %s", e)
