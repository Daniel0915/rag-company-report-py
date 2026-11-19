"""수집 실행 루프: 잠금, 재시도 목록 우선 처리, 기업 x 유형 순회, 한도 초과 중단, 상태 저장."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from app import watchlist
from app.config import Settings
from app.dart.dto import DisclosureItem
from app.dart.errors import DartApiError
from app.ingest.ledger import IndexLedger
from app.ingest.lock import ingest_lock
from app.ingest.state import (
    IngestState,
    PendingFailure,
    load_state,
    record_failure,
    record_success,
    save_state,
    update_window,
)
from app.models import IndexResult
from app.services.company_report_index import ALL_DISCLOSURE_TYPES, CompanyReportIndexService
from app.watchlist import WatchedCompany

logger = logging.getLogger("app.ingest")

RATE_LIMIT_STATUS = "020"
RATE_LIMIT_REASON = "DART 하루 호출 한도 초과"


@dataclass
class RunSummary:
    new: int = 0
    empty: int = 0
    skipped: int = 0
    failed: int = 0
    given_up: int = 0
    chunks: int = 0
    aborted_reason: str | None = None
    results: list[IndexResult] = field(default_factory=list)


def _is_rate_limit(e: Exception) -> bool:
    return isinstance(e, DartApiError) and e.status == RATE_LIMIT_STATUS


class IngestRunner:
    def __init__(
        self, index_service: CompanyReportIndexService, ledger: IndexLedger, settings: Settings
    ) -> None:
        self._index = index_service
        self._ledger = ledger
        self._settings = settings

    def run_update(self, today: date | None = None) -> RunSummary:
        today = today or date.today()
        state = load_state(self._settings.ingest_state_file)
        bgn_de, end_de = update_window(state, today)
        return self.run(bgn_de, end_de, today=today)

    def run(
        self,
        bgn_de: date,
        end_de: date,
        corp_codes: list[str] | None = None,
        types: list[str] | None = None,
        today: date | None = None,
        advance_success_date: bool = True,
    ) -> RunSummary:
        today = today or date.today()
        types = types or list(ALL_DISCLOSURE_TYPES)
        targets = [
            c for c in watchlist.WATCHLIST if corp_codes is None or c.corp_code in corp_codes
        ]
        summary = RunSummary()

        with ingest_lock(self._settings.ingest_lock_file):
            self._ledger.reload()
            state = load_state(self._settings.ingest_state_file)
            state.last_run_at = datetime.now().isoformat(timespec="seconds")
            try:
                self._run_locked(state, summary, bgn_de, end_de, targets, types)
                if summary.aborted_reason is None and advance_success_date:
                    state.last_success_date = today
            finally:
                save_state(self._settings.ingest_state_file, state)
        logger.info(
            "수집 요약: 신규 %d, 빈 공시 %d, 건너뜀 %d, 실패 %d, 포기 %d, 청크 %d, 중단 %s",
            summary.new, summary.empty, summary.skipped, summary.failed,
            summary.given_up, summary.chunks, summary.aborted_reason,
        )
        return summary

    def _run_locked(
        self,
        state: IngestState,
        summary: RunSummary,
        bgn_de: date,
        end_de: date,
        targets: list[WatchedCompany],
        types: list[str],
    ) -> None:
        target_codes = {c.corp_code for c in targets}
        done: set[str] = set()

        for p in list(state.pending):
            company = watchlist.find(p.corp_code)
            if company is None or p.corp_code not in target_codes or p.pblntf_ty not in types:
                logger.info("재시도 건너뜀(대상 아님): %s %s", p.rcept_no, p.corp_code)
                continue
            retry = DisclosureItem(
                corp_code=p.corp_code, report_nm=p.report_nm,
                rcept_no=p.rcept_no, rcept_dt=p.rcept_dt,
            )
            done.add(p.rcept_no)
            if self._process(state, summary, company, retry, p.pblntf_ty):
                return

        given_up = {p.rcept_no for p in state.given_up}
        for company in targets:
            for ty in types:
                try:
                    items = self._index.list_disclosures(company.corp_code, bgn_de, end_de, ty)
                except Exception as e:
                    if _is_rate_limit(e):
                        summary.aborted_reason = RATE_LIMIT_REASON
                        logger.error("%s: 목록 조회 중 호출 한도 초과", company.name)
                        return
                    summary.failed += 1
                    logger.error("목록 조회 실패 %s/%s: %s", company.name, ty, e)
                    continue
                for item in items:
                    if item.rcept_no in done:
                        continue
                    if item.rcept_no in given_up:
                        logger.info("재시도 포기한 공시라 건너뜀: %s", item.rcept_no)
                        continue
                    done.add(item.rcept_no)
                    if self._process(state, summary, company, item, ty):
                        return

    def _process(
        self,
        state: IngestState,
        summary: RunSummary,
        company: WatchedCompany,
        item: DisclosureItem,
        ty: str,
    ) -> bool:
        """공시 하나를 색인한다. 호출 한도 초과로 중단해야 하면 True."""
        try:
            result = self._index.index_disclosure(company, item, ty)
        except Exception as e:
            if _is_rate_limit(e):
                summary.aborted_reason = RATE_LIMIT_REASON
                logger.error("%s %s: 호출 한도 초과", company.name, item.rcept_no)
                return True
            record_failure(state, PendingFailure(
                rcept_no=item.rcept_no, corp_code=company.corp_code, pblntf_ty=ty,
                report_nm=item.report_nm, rcept_dt=item.rcept_dt, error=str(e), attempts=0,
            ))
            if any(g.rcept_no == item.rcept_no for g in state.given_up):
                summary.given_up += 1
            else:
                summary.failed += 1
            logger.error("색인 실패 %s %s: %s", company.name, item.rcept_no, e)
            return False

        record_success(state, item.rcept_no)
        if result.status == "new":
            summary.new += 1
        elif result.status == "empty":
            summary.empty += 1
        elif result.status == "skipped":
            summary.skipped += 1
        summary.chunks += result.chunks
        summary.results.append(result)
        logger.info(
            "%s %s %s: %s (%d청크)",
            company.name, item.rcept_no, item.report_nm, result.status, result.chunks,
        )
        return False
