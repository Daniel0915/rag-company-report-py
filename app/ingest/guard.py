"""기록 파일이 비었는데 Neo4j에 공시가 이미 있을 때 중복 색인을 막는 점검."""

from __future__ import annotations

from neo4j import Driver

from app.ingest.ledger import IndexLedger

_DISCLOSURE_NODES = (
    "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'CompanyReportChunk_') "
    "AND n.`metadata.doc_type` IS NULL"
)


def ledger_guard(ledger: IndexLedger, disclosure_chunk_count: int) -> str | None:
    """기록이 비었는데 Neo4j에 공시 청크가 있으면 중복 색인을 막기 위해 거부 메시지를 돌려준다."""
    if ledger.is_empty() and disclosure_chunk_count > 0:
        return (
            "기록 파일이 비어 있는데 Neo4j에 공시 청크가 있습니다. "
            "먼저 python -m app.ingest import-ledger <Spring 기록 파일>을 실행하세요"
        )
    return None


def count_disclosure_chunks(driver: Driver) -> int:
    with driver.session() as session:
        return session.run(f"{_DISCLOSURE_NODES} RETURN count(n) AS total").single()["total"]


def count_disclosure_rcept_nos(driver: Driver) -> int:
    query = f"{_DISCLOSURE_NODES} RETURN count(DISTINCT n.`metadata.rcept_no`) AS total"
    with driver.session() as session:
        return session.run(query).single()["total"]
