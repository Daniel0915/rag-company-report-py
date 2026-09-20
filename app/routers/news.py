from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import Container, get_container
from app.models import IndexResult

router = APIRouter(prefix="/api/admin/news")


@router.post("/fetch", response_model=list[IndexResult])
def fetch(container: Container = Depends(get_container)) -> list[IndexResult]:
    """워치리스트 전체 기업의 최근 뉴스를 네이버 뉴스검색에서 가져와 색인한다."""
    return container.news_service.fetch_watched_company_news()
