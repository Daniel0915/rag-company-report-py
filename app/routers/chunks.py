from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.dependencies import Container, get_container
from app.models import IndexedChunkPage

router = APIRouter()


@router.get("/api/admin/indexed-chunks", response_model=IndexedChunkPage)
def indexed_chunks(
    corpCode: str | None = Query(default=None),
    sourceType: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    container: Container = Depends(get_container),
) -> IndexedChunkPage:
    """어드민 뷰어: 실제로 Neo4j에 색인된 청크를 기업/공시유형으로 필터링해서 조회한다."""
    return container.chunk_service.list_chunks(
        corpCode, sourceType, min(max(limit, 1), 200), max(offset, 0)
    )
