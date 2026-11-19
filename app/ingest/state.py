"""수집 실행 상태 파일(마지막 실행·성공일, 재시도 목록, 포기 목록)과 조회 기간 계산."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

OVERLAP_DAYS = 7
MAX_ATTEMPTS = 5


@dataclass
class PendingFailure:
    rcept_no: str
    corp_code: str
    pblntf_ty: str
    report_nm: str
    rcept_dt: str
    error: str
    attempts: int


@dataclass
class IngestState:
    last_run_at: str | None = None
    last_success_date: date | None = None
    pending: list[PendingFailure] = field(default_factory=list)
    given_up: list[PendingFailure] = field(default_factory=list)


def load_state(path: str | Path) -> IngestState:
    p = Path(path)
    if not p.exists():
        return IngestState()
    raw = json.loads(p.read_text(encoding="utf-8"))
    last_success = raw.get("last_success_date")
    return IngestState(
        last_run_at=raw.get("last_run_at"),
        last_success_date=date.fromisoformat(last_success) if last_success else None,
        pending=[PendingFailure(**d) for d in raw.get("pending", [])],
        given_up=[PendingFailure(**d) for d in raw.get("given_up", [])],
    )


def save_state(path: str | Path, state: IngestState) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(state)
    data["last_success_date"] = (
        state.last_success_date.isoformat() if state.last_success_date else None
    )
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def update_window(state: IngestState, today: date) -> tuple[date, date]:
    if state.last_success_date is not None:
        return state.last_success_date - timedelta(days=OVERLAP_DAYS), today
    try:
        start = today.replace(year=today.year - 2)
    except ValueError:  # 2월 29일
        start = today.replace(year=today.year - 2, day=28)
    return start, today


def record_failure(state: IngestState, item: PendingFailure) -> None:
    existing = next((p for p in state.pending if p.rcept_no == item.rcept_no), None)
    if existing is None:
        item.attempts = 1
        state.pending.append(item)
        existing = item
    else:
        existing.attempts += 1
        existing.error = item.error
    if existing.attempts >= MAX_ATTEMPTS:
        state.pending.remove(existing)
        state.given_up.append(existing)


def record_success(state: IngestState, rcept_no: str) -> None:
    state.pending = [p for p in state.pending if p.rcept_no != rcept_no]
