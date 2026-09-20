from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import Container, get_container
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
    """워치리스트 기업의 정기공시+지분공시를 색인한다. 기간 미지정 시 최근 2년치."""
    end = date.fromisoformat(end_de) if end_de else date.today()
    begin = date.fromisoformat(bgn_de) if bgn_de else end.replace(year=end.year - 2)
    return container.index_service.index_watched_companies(begin, end)


@router.post("/api/company-report/chat", response_model=ChatResponse)
def chat(request: ChatRequest, container: Container = Depends(get_container)) -> ChatResponse:
    top_k = 4 if request.topK <= 0 else min(max(request.topK, 1), 20)
    try:
        return container.chat_service.chat(
            request.question, request.corpCode, top_k, request.history, request.provider
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
