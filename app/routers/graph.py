"""
지분공시 그래프(제출인-공시-기업) 탐색용 API. 벡터 검색으로는 안 되는 멀티홉 질의
(예: "이 사람이 지분을 공시한 다른 회사는?")를 위한 것.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.dependencies import Container, get_container
from app.models import FilerDisclosureDto, RelatedCompanyDto

router = APIRouter(prefix="/api/admin/graph")


@router.get("/filers", response_model=list[FilerDisclosureDto])
def filers(
    corpCode: str = Query(...),
    limit: int = Query(default=20),
    container: Container = Depends(get_container),
) -> list[FilerDisclosureDto]:
    return container.disclosure_graph.find_filers(corpCode, min(max(limit, 1), 100))


@router.get("/related-companies", response_model=list[RelatedCompanyDto])
def related_companies(
    filerName: str = Query(...),
    limit: int = Query(default=20),
    container: Container = Depends(get_container),
) -> list[RelatedCompanyDto]:
    return container.disclosure_graph.find_related_companies(filerName, min(max(limit, 1), 100))
