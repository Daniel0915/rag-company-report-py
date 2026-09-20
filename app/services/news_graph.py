"""
뉴스 메타데이터(기업/기사/언론사)를 그래프로 구성한다. DisclosureGraphService와 같은
Company 노드를 재사용해서, 공시 그래프와 뉴스 그래프가 한 기업 노드에서 만나게 한다.

(:Media)-[:PUBLISHED]->(:Article)-[:ABOUT]->(:Company)
(:Article)-[:HAS_CHUNK]->(청크 노드, CompanyReportChunk_{corpCode})

청크 자체는 기업별 벡터 인덱스에 doc_type='NEWS'로 그대로 합류하고(별도 라벨 안 만듦),
이 서비스는 그 청크 노드를 Article과 연결하는 그래프 관계만 추가로 만든다.
"""
from __future__ import annotations

from neo4j import Driver

from app.vector.registry import label_for
from app.watchlist import WatchedCompany

_MERGE_ARTICLE = """
MERGE (co:Company {corp_code: $corpCode})
  ON CREATE SET co.name = $corpName, co.stock_code = $stockCode
MERGE (a:Article {url: $url})
  ON CREATE SET a.title = $title, a.published_date = $publishedDate
MERGE (a)-[:ABOUT]->(co)
"""

_MERGE_MEDIA = """
MATCH (a:Article {url: $url})
MERGE (m:Media {name: $press})
MERGE (m)-[:PUBLISHED]->(a)
"""


class NewsGraphService:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def record_article(
        self,
        company: WatchedCompany,
        url: str,
        title: str,
        press: str | None,
        published_date: str | None,
        chunk_ids: list[str],
    ) -> None:
        """기사 1건을 그래프에 반영한다. url이 유일키."""
        with self._driver.session() as session:
            session.run(
                _MERGE_ARTICLE,
                corpCode=company.corp_code,
                corpName=company.name,
                stockCode=company.stock_code,
                url=url,
                title=title,
                publishedDate=published_date,
            )

            if press and press.strip():
                session.run(_MERGE_MEDIA, url=url, press=press)

            if chunk_ids:
                link_query = (
                    "MATCH (a:Article {url: $url}) "
                    f"MATCH (c:`{label_for(company.corp_code)}`) WHERE c.id IN $chunkIds "
                    "MERGE (a)-[:HAS_CHUNK]->(c)"
                )
                session.run(link_query, url=url, chunkIds=chunk_ids)
