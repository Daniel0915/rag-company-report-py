"""
어드민 페이지에서 Neo4j에 실제로 색인된 청크를 훑어보기 위한 읽기 전용 뷰어.
벡터스토어는 유사도 검색만 지원하므로, 저장된 데이터를 있는 그대로 브라우징하려면
Neo4j 드라이버로 청크 노드를 직접 조회해야 한다. 기업마다 라벨이 분리돼 있어서,
특정 기업이 지정되면 그 라벨만 조회하고 "전체"면 워치리스트의 모든 라벨을 조회해 합친다.
"""
from __future__ import annotations

from neo4j import Driver

from app.models import IndexedChunkDto, IndexedChunkPage
from app.vector.registry import label_for
from app.watchlist import WATCHLIST

_RETURN_CLAUSE = """
RETURN n.id AS id,
       n.`metadata.corp_code` AS corpCode,
       n.`metadata.corp_name` AS corpName,
       n.`metadata.report_nm` AS reportNm,
       n.`metadata.rcept_no` AS rceptNo,
       n.`metadata.pblntf_ty` AS pblntfTy,
       n.`metadata.doc_type` AS docType,
       n.`metadata.section_title` AS sectionTitle,
       n.`metadata.rcept_dt` AS rceptDt,
       left(n.text, 200) AS textPreview
ORDER BY coalesce(n.`metadata.rcept_dt`, '') DESC, coalesce(n.`metadata.section_order`, 0) ASC
SKIP $skip LIMIT $limit
"""


def _type_condition(source_type: str | None) -> str:
    # pblntf_ty 태깅을 추가하기 전에 이미 색인된 정기공시 청크는 이 필드가 아예 없다.
    # "정기공시"로 필터링할 때 그런 레거시 청크가 누락되지 않도록, doc_type도 없는
    # (=관리자 PDF가 아닌) 청크는 정기공시로 간주해서 같이 포함한다.
    return {
        "A": "(n.`metadata.pblntf_ty` = 'A' OR (n.`metadata.pblntf_ty` IS NULL AND n.`metadata.doc_type` IS NULL))",
        "D": "n.`metadata.pblntf_ty` = 'D'",
        "ADMIN_PDF": "n.`metadata.doc_type` = 'ADMIN_PDF'",
        "NEWS": "n.`metadata.doc_type` = 'NEWS'",
    }.get(source_type or "", "true")


class IndexedChunkService:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def list_chunks(
        self, corp_code: str | None, source_type: str | None, limit: int, offset: int
    ) -> IndexedChunkPage:
        corp_codes = [corp_code] if corp_code else [c.corp_code for c in WATCHLIST]
        type_condition = _type_condition(source_type)

        total = sum(self._count_label(label_for(code), type_condition) for code in corp_codes)

        if len(corp_codes) == 1:
            items = self._query_label(label_for(corp_codes[0]), type_condition, offset, limit)
        else:
            # 여러 라벨(전체 기업)을 합쳐야 하므로, 각 라벨에서 필요한 만큼(offset+limit)
            # 최신순으로 후보를 뽑은 뒤 애플리케이션에서 다시 정렬해 페이지를 잘라낸다.
            merged: list[IndexedChunkDto] = []
            for code in corp_codes:
                merged.extend(self._query_label(label_for(code), type_condition, 0, offset + limit))
            merged.sort(key=lambda d: d.rceptDt or "", reverse=True)
            items = merged[offset : offset + limit]

        return IndexedChunkPage(items=items, total=total)

    def _query_label(
        self, label: str, type_condition: str, skip: int, limit: int
    ) -> list[IndexedChunkDto]:
        query = f"MATCH (n:`{label}`) WHERE {type_condition} {_RETURN_CLAUSE}"
        with self._driver.session() as session:
            records = list(session.run(query, skip=skip, limit=limit))
        return [IndexedChunkDto(**dict(record)) for record in records]

    def _count_label(self, label: str, type_condition: str) -> int:
        query = f"MATCH (n:`{label}`) WHERE {type_condition} RETURN count(n) AS total"
        with self._driver.session() as session:
            return session.run(query).single()["total"]
