"""
워치리스트 기업마다 네이버 뉴스 검색결과 페이지(NaverSearchScraper, API 키 불필요)에서
기사 URL을 찾고, 본문을 크롤링(NaverArticleScraper)해서 청킹한 뒤 기업별 벡터 인덱스에
doc_type='NEWS'로 합류시킨다. CompanyReportIndexService와 같은 메타데이터 키
(report_nm/rcept_no/rcept_dt/section_title)를 써서 CompanyChatService가 코드 변경 없이
뉴스도 그대로 근거로 쓸 수 있게 한다.

기사 url을 유일키로 이미 색인한 건 스킵한다. 본문 크롤링에 실패한 URL은 실패로 기록만 하고
seen_urls에는 안 남겨서 다음 실행 때 다시 시도한다(일시적 오류일 수 있으므로).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.models import IndexResult
from app.naver.article_scraper import NaverArticleScraper
from app.naver.search_scraper import NaverSearchScraper
from app.services.news_graph import NewsGraphService
from app.vector.document import Document
from app.vector.registry import CompanyVectorStoreRegistry
from app.vector.splitter import TokenTextSplitter
from app.watchlist import WATCHLIST, WatchedCompany

DELAY_SECONDS = 0.4


class NewsIngestService:
    def __init__(
        self,
        search_scraper: NaverSearchScraper,
        article_scraper: NaverArticleScraper,
        registry: CompanyVectorStoreRegistry,
        news_graph: NewsGraphService,
        sidecar_path: str,
        articles_per_company: int,
    ) -> None:
        self._search = search_scraper
        self._article = article_scraper
        self._registry = registry
        self._graph = news_graph
        self._splitter = TokenTextSplitter()
        self._sidecar_file = Path(sidecar_path)
        self._sidecar_file.parent.mkdir(parents=True, exist_ok=True)
        self._seen_urls = self._load_sidecar()
        self._articles_per_company = articles_per_company
        self._lock = threading.Lock()

    def fetch_watched_company_news(self) -> list[IndexResult]:
        with self._lock:
            results: list[IndexResult] = []
            for company in WATCHLIST:
                results.extend(self._fetch_company_news(company))
            return results

    def _fetch_company_news(self, company: WatchedCompany) -> list[IndexResult]:
        urls = self._search.search_article_urls(company.name, self._articles_per_company)

        results: list[IndexResult] = []
        for url in urls:
            if url in self._seen_urls:
                results.append(
                    IndexResult(
                        corpName=company.name, rceptNo=url, reportNm=url, status="skipped", chunks=0
                    )
                )
                continue
            results.append(self._ingest_article(company, url))
            time.sleep(DELAY_SECONDS)
        return results

    def _ingest_article(self, company: WatchedCompany, url: str) -> IndexResult:
        article = self._article.fetch_article(url)
        if article is None:
            return IndexResult(
                corpName=company.name, rceptNo=url, reportNm=url, status="failed", chunks=0
            )

        title = article.title or url
        tag_meta = {
            "doc_type": "NEWS",
            "corp_name": company.name,
            "corp_code": company.corp_code,
            "stock_code": company.stock_code,
            "report_nm": title,
            "rcept_no": url,
            "section_title": article.press or "뉴스",
            "article_url": url,
        }
        if article.published_date:
            tag_meta["rcept_dt"] = article.published_date

        document = Document(text=f"{title}\n\n{article.content}", metadata=tag_meta)
        chunks = self._splitter.apply([document])
        self._registry.for_company(company.corp_code).add(chunks)

        self._graph.record_article(
            company, url, title, article.press, article.published_date, [c.id for c in chunks]
        )

        self._seen_urls.add(url)
        self._persist_sidecar()

        return IndexResult(
            corpName=company.name, rceptNo=url, reportNm=title, status="new", chunks=len(chunks)
        )

    def _load_sidecar(self) -> set[str]:
        if not self._sidecar_file.exists():
            return set()
        try:
            return set(json.loads(self._sidecar_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return set()

    def _persist_sidecar(self) -> None:
        try:
            self._sidecar_file.write_text(
                json.dumps(sorted(self._seen_urls), ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            raise RuntimeError("뉴스 색인 사이드카 저장 실패") from e
