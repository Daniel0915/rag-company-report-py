"""
공시 메타데이터(기업/공시/제출인)를 그래프로 구성해서, 벡터 유사도 검색만으로는
답할 수 없는 다중 홉(multi-hop) 질의를 지원한다 (예: "이 사람이 지분을 공시한 다른 회사는?").
DART list.json이 이미 제공하는 flr_nm(제출인)만 사용하므로 별도 텍스트/표 파싱이나
LLM 추출 없이도 신뢰도 높은 그래프를 만들 수 있다.

벡터 청크와는 별개의 그래프(:Company, :Report, :Filer)이며, 기존 벡터 검색 파이프라인에는
영향을 주지 않는다.
"""
from __future__ import annotations

from neo4j import Driver

from app.dart.dto import DisclosureItem
from app.models import FilerDisclosureDto, GraphExpansionRow, RelatedCompanyDto
from app.watchlist import WatchedCompany

_RECORD_DISCLOSURE = """
MERGE (co:Company {corp_code: $corpCode})
  ON CREATE SET co.name = $corpName, co.stock_code = $stockCode
MERGE (r:Report {rcept_no: $rceptNo})
  ON CREATE SET r.report_nm = $reportNm, r.rcept_dt = $rceptDt, r.pblntf_ty = $pblntfTy
MERGE (r)-[:FILED_BY]->(co)
"""

_RECORD_FILER = """
MATCH (r:Report {rcept_no: $rceptNo})
MERGE (f:Filer {name: $filerName})
MERGE (f)-[:DISCLOSED]->(r)
"""

_FIND_FILERS = """
MATCH (f:Filer)-[:DISCLOSED]->(r:Report)-[:FILED_BY]->(co:Company {corp_code: $corpCode})
RETURN f.name AS filerName, r.report_nm AS reportNm, r.rcept_dt AS rceptDt, r.rcept_no AS rceptNo
ORDER BY r.rcept_dt DESC
LIMIT $limit
"""

_FIND_RELATED_COMPANIES = """
MATCH (f:Filer {name: $filerName})-[:DISCLOSED]->(:Report)-[:FILED_BY]->(co:Company)
RETURN DISTINCT co.corp_code AS corpCode, co.name AS corpName
LIMIT $limit
"""

_EXPAND_RETRIEVED_CONTEXT = """
CALL {
    UNWIND $rceptNos AS rceptNo
    MATCH (f:Filer)-[:DISCLOSED]->(r:Report {rcept_no: rceptNo})-[:FILED_BY]->(:Company {corp_code: $corpCode})
    OPTIONAL MATCH (f)-[:DISCLOSED]->(:Report)-[:FILED_BY]->(other:Company)
    WHERE other.corp_code <> $corpCode
    RETURN 'DISCLOSURE' AS kind, rceptNo AS anchor, f.name AS primaryName,
           r.report_nm AS secondaryName, other.corp_code AS relatedCorpCode, other.name AS relatedCorpName

    UNION ALL

    UNWIND $articleUrls AS articleUrl
    MATCH (a:Article {url: articleUrl})-[:ABOUT]->(other:Company)
    WHERE other.corp_code <> $corpCode
    OPTIONAL MATCH (m:Media)-[:PUBLISHED]->(a)
    RETURN 'NEWS' AS kind, articleUrl AS anchor, coalesce(m.name, '(언론사 미상)') AS primaryName,
           a.title AS secondaryName, other.corp_code AS relatedCorpCode, other.name AS relatedCorpName
}
RETURN DISTINCT kind, anchor, primaryName, secondaryName, relatedCorpCode, relatedCorpName
LIMIT $limit
"""


class DisclosureGraphService:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def record_disclosure(
        self, company: WatchedCompany, item: DisclosureItem, pblntf_ty: str
    ) -> None:
        """공시 1건을 그래프에 반영한다. 모든 공시는 Report-Company로, 지분공시(D)는 제출인(Filer)까지 연결."""
        params = {
            "corpCode": company.corp_code,
            "corpName": company.name,
            "stockCode": company.stock_code,
            "rceptNo": item.rcept_no,
            "reportNm": item.report_nm,
            "rceptDt": item.rcept_dt,
            "pblntfTy": pblntf_ty,
        }
        with self._driver.session() as session:
            session.run(_RECORD_DISCLOSURE, **params)

            if pblntf_ty == "D" and item.flr_nm.strip() and item.flr_nm != company.name:
                session.run(_RECORD_FILER, rceptNo=item.rcept_no, filerName=item.flr_nm)

    def find_filers(self, corp_code: str, limit: int) -> list[FilerDisclosureDto]:
        """특정 기업에 지분을 공시한 제출인 목록(최근순)."""
        with self._driver.session() as session:
            records = list(session.run(_FIND_FILERS, corpCode=corp_code, limit=limit))
        return [
            FilerDisclosureDto(
                filerName=r["filerName"],
                reportNm=r["reportNm"] or "",
                rceptDt=r["rceptDt"] or "",
                rceptNo=r["rceptNo"] or "",
            )
            for r in records
        ]

    def find_related_companies(self, filer_name: str, limit: int) -> list[RelatedCompanyDto]:
        """특정 제출인이 지분을 공시한 다른 회사들 (멀티홉 탐색)."""
        with self._driver.session() as session:
            records = list(session.run(_FIND_RELATED_COMPANIES, filerName=filer_name, limit=limit))
        return [
            RelatedCompanyDto(corpCode=r["corpCode"] or "", corpName=r["corpName"] or "")
            for r in records
        ]

    def run_read_only_query(self, cypher: str) -> str | None:
        """
        LLM(Text2Cypher)이 생성한 쿼리를 읽기 전용 세션으로 실행한다. 세션 자체를 READ
        접근 모드로 열어서, 정규식 검사를 통과한 악의적/실수 쓰기 쿼리라도 Neo4j 서버 단에서
        다시 한번 거부되도록 이중으로 막는다. 결과가 없으면 None을 반환해 호출부가
        고정 쿼리로 폴백할 수 있게 한다.
        """
        with self._driver.session(default_access_mode="READ") as session:
            records = list(session.run(cypher))
        if not records:
            return None
        return "\n".join(
            "- " + ", ".join(f"{key}={record[key]}" for key in record.keys())
            for record in records
        )

    def expand_retrieved_context(
        self, corp_code: str, rcept_nos: list[str], article_urls: list[str], limit: int
    ) -> list[GraphExpansionRow]:
        """
        VectorCypher: 벡터 검색이 이미 뽑은 topK 청크의 메타데이터(rcept_no, article_url)에
        고정 앵커된 단일 Cypher로, 청크 자체엔 없는 그래프 전용 정보를 보강한다. 쿼리
        문자열이 고정/파라미터화돼 있고 LLM이 만든 게 아니므로 run_read_only_query처럼
        쓰기 구문 검사나 READ 전용 세션이 필요 없다 - find_filers와 같은 신뢰 수준의 고정 조회다.
        """
        if not rcept_nos and not article_urls:
            return []
        with self._driver.session() as session:
            records = list(
                session.run(
                    _EXPAND_RETRIEVED_CONTEXT,
                    corpCode=corp_code,
                    rceptNos=rcept_nos,
                    articleUrls=article_urls,
                    limit=limit,
                )
            )
        return [
            GraphExpansionRow(
                kind=r["kind"] or "",
                anchor=r["anchor"] or "",
                primaryName=r["primaryName"] or "",
                secondaryName=r["secondaryName"] or "",
                relatedCorpCode=r["relatedCorpCode"],
                relatedCorpName=r["relatedCorpName"],
            )
            for r in records
        ]
