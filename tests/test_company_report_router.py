from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_container
from app.ingest.lock import IngestBusyError
from app.ingest.runner import RunSummary
from app.models import IndexResult
from app.routers import company_report


def _client(runner: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(company_report.router)
    app.dependency_overrides[get_container] = lambda: SimpleNamespace(ingest_runner=runner)
    return TestClient(app)


def _result() -> IndexResult:
    return IndexResult(
        corpName="삼성전자", rceptNo="20260101000001", reportNm="사업보고서", status="NEW", chunks=3
    )


def test_index_without_dates_runs_update():
    runner = MagicMock()
    runner.run_update.return_value = RunSummary(results=[_result()])

    res = _client(runner).post("/api/company-report/index")

    assert res.status_code == 200
    assert res.json() == [_result().model_dump()]
    runner.run_update.assert_called_once_with()
    runner.run.assert_not_called()


def test_index_with_dates_runs_range():
    runner = MagicMock()
    runner.run.return_value = RunSummary(results=[_result()])

    res = _client(runner).post("/api/company-report/index?bgn_de=2026-01-01&end_de=2026-01-31")

    assert res.status_code == 200
    runner.run.assert_called_once_with(
        date(2026, 1, 1), date(2026, 1, 31), advance_success_date=False
    )
    runner.run_update.assert_not_called()


def test_index_returns_409_when_busy():
    runner = MagicMock()
    runner.run_update.side_effect = IngestBusyError("다른 수집이 실행 중입니다")

    res = _client(runner).post("/api/company-report/index")

    assert res.status_code == 409
    assert res.json()["detail"] == "다른 수집이 실행 중입니다"


def test_index_returns_503_when_aborted():
    runner = MagicMock()
    runner.run_update.return_value = RunSummary(aborted_reason="DART 하루 호출 한도 초과")

    res = _client(runner).post("/api/company-report/index")

    assert res.status_code == 503
    assert res.json()["detail"] == "DART 하루 호출 한도 초과"


def test_index_rejects_single_date():
    runner = MagicMock()
    client = _client(runner)

    for qs in ("bgn_de=2026-01-01", "end_de=2026-01-31"):
        res = client.post(f"/api/company-report/index?{qs}")
        assert res.status_code == 422
        assert "bgn_de" in res.json()["detail"]
    runner.run.assert_not_called()
    runner.run_update.assert_not_called()
