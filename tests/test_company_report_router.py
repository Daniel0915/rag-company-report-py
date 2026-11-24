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


def test_index_returns_503_when_ledger_guard_refuses():
    message = "기록 파일이 비어 있는데 Neo4j에 공시 청크가 있습니다. 먼저 import-ledger를 실행하세요"
    runner = MagicMock()
    runner.run_update.return_value = RunSummary(aborted_reason=message)

    res = _client(runner).post("/api/company-report/index")

    assert res.status_code == 503
    assert res.json()["detail"] == message


def test_index_rejects_single_date():
    runner = MagicMock()
    client = _client(runner)

    for qs in ("bgn_de=2026-01-01", "end_de=2026-01-31"):
        res = client.post(f"/api/company-report/index?{qs}")
        assert res.status_code == 422
        assert "bgn_de" in res.json()["detail"]
    runner.run.assert_not_called()
    runner.run_update.assert_not_called()


def test_index_rejects_malformed_date():
    runner = MagicMock()

    res = _client(runner).post("/api/company-report/index?bgn_de=2026-13-01&end_de=2026-01-31")

    assert res.status_code == 422
    assert "YYYY-MM-DD" in res.json()["detail"]
    runner.run.assert_not_called()
    runner.run_update.assert_not_called()


def test_index_runner_value_error_is_not_masked_as_422():
    runner = MagicMock()
    runner.run_update.side_effect = ValueError("손상된 상태 파일")
    app = FastAPI()
    app.include_router(company_report.router)
    app.dependency_overrides[get_container] = lambda: SimpleNamespace(ingest_runner=runner)

    res = TestClient(app, raise_server_exceptions=False).post("/api/company-report/index")

    assert res.status_code == 500


def _chat_client(chat_service=None, agent_service=None) -> TestClient:
    app = FastAPI()
    app.include_router(company_report.router)
    container = SimpleNamespace(chat_service=chat_service, agent_service=agent_service)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def test_non_agent_response_json_unchanged():
    from app.models import ChatResponse, SourceItem

    chat = MagicMock()
    chat.chat.return_value = ChatResponse(
        answer="a",
        sources=[SourceItem(corpName="c", reportNm="r", rceptNo="1", sectionTitle="s", docType=None)],
    )
    res = _chat_client(chat_service=chat).post(
        "/api/company-report/chat", json={"question": "q", "corpCode": "00126380"}
    )
    assert set(res.json()) == {"answer", "sources"}
    assert set(res.json()["sources"][0]) == {"corpName", "reportNm", "rceptNo", "sectionTitle", "docType"}
    assert res.json()["sources"][0]["docType"] is None


def test_agent_request_routes_to_agent_service():
    from app.models import ChatResponse, ToolCallTrace

    agent = MagicMock()
    agent.chat.return_value = ChatResponse(
        answer="a", sources=[],
        toolCalls=[ToolCallTrace(name="calculate", arguments={"expression": "1+1"}, ok=True, summary="2")],
    )
    chat = MagicMock()
    res = _chat_client(chat_service=chat, agent_service=agent).post(
        "/api/company-report/chat",
        json={"question": "q", "corpCode": "00126380", "agent": True, "provider": "claude"},
    )
    assert res.status_code == 200
    assert res.json()["toolCalls"][0]["name"] == "calculate"
    agent.chat.assert_called_once_with("q", "00126380", [], "claude")
    chat.chat.assert_not_called()


def _agent_post(agent):
    return _chat_client(agent_service=agent).post(
        "/api/company-report/chat", json={"question": "q", "corpCode": "00126380", "agent": True}
    )


def test_agent_failure_returns_502_without_leaking_message():
    agent = MagicMock()
    agent.chat.side_effect = RuntimeError("claude CLI 응답이 180초 안에 오지 않았습니다 secret")

    res = _agent_post(agent)

    assert res.status_code == 502
    assert res.json()["detail"] == "에이전트 실행 실패"
    assert "secret" not in res.text


def test_agent_empty_corp_code_returns_400_without_calling_agent():
    agent = MagicMock()
    res = _chat_client(agent_service=agent).post(
        "/api/company-report/chat", json={"question": "q", "corpCode": "  ", "agent": True}
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "corpCode는 필수입니다"
    agent.chat.assert_not_called()


def test_agent_internal_value_error_maps_to_502_without_internal_text():
    agent = MagicMock()
    agent.chat.side_effect = ValueError("Expecting value: line 1 column 1 (char 0) internal")

    res = _agent_post(agent)

    assert res.status_code == 502
    assert res.json()["detail"] == "에이전트 실행 실패"
    assert "internal" not in res.text


def test_agent_web_source_serializes_all_source_keys():
    from app.models import ChatResponse, SourceItem

    agent = MagicMock()
    agent.chat.return_value = ChatResponse(
        answer="a",
        sources=[SourceItem(
            corpName=None, reportNm="기사", rceptNo="http://x", sectionTitle=None, docType="WEB"
        )],
        toolCalls=[],
    )

    source = _agent_post(agent).json()["sources"][0]

    assert set(source) == {"corpName", "reportNm", "rceptNo", "sectionTitle", "docType"}
    assert source["docType"] == "WEB"
