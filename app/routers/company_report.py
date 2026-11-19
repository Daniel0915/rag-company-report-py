from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import Container, get_container
from app.ingest.lock import IngestBusyError
from app.models import ChatRequest, ChatResponse, IndexResult, WatchedCompanyDto
from app.watchlist import WATCHLIST

router = APIRouter()


@router.get("/api/company-report/watchlist", response_model=list[WatchedCompanyDto])
def watchlist() -> list[WatchedCompanyDto]:
    return [
        WatchedCompanyDto(name=c.name, corpCode=c.corp_code, stockCode=c.stock_code)
        for c in WATCHLIST
    ]


@router.post("/api/company-report/index", response_model=list[IndexResult])
def index(
    bgn_de: str | None = Query(default=None, alias="bgn_de"),
    end_de: str | None = Query(default=None, alias="end_de"),
    container: Container = Depends(get_container),
) -> list[IndexResult]:
    """전 유형 공시를 색인한다. 기간 미지정이면 마지막 성공일 이후분을 이어 받고(성공일 갱신),
    기간을 주면 그 범위만 다시 훑는다(성공일은 건드리지 않음). 다른 수집이 돌고 있으면 409,
    DART 한도 초과 등으로 중단되면 503."""
    if (bgn_de is None) != (end_de is None):
        raise HTTPException(status_code=422, detail="bgn_de와 end_de는 함께 지정해야 합니다.")
    try:
        begin = date.fromisoformat(bgn_de) if bgn_de else None
        end = date.fromisoformat(end_de) if end_de else None
    except ValueError as e:
        raise HTTPException(status_code=422, detail="날짜는 YYYY-MM-DD 형식이어야 합니다.") from e
    try:
        if begin is None:
            summary = container.ingest_runner.run_update()
        else:
            summary = container.ingest_runner.run(begin, end, advance_success_date=False)
    except IngestBusyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if summary.aborted_reason:
        raise HTTPException(status_code=503, detail=summary.aborted_reason)
    return summary.results


@router.post("/api/company-report/chat", response_model=ChatResponse)
def chat(request: ChatRequest, container: Container = Depends(get_container)) -> ChatResponse:
    top_k = 4 if request.topK <= 0 else min(max(request.topK, 1), 20)
    try:
        return container.chat_service.chat(
            request.question, request.corpCode, top_k, request.history, request.provider
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
