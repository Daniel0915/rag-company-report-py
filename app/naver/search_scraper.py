"""
네이버 API 키 없이, 네이버 뉴스 검색결과 페이지(search.naver.com)를 직접 파싱해서
기사 URL 목록만 뽑는다. 검색결과 페이지의 CSS 클래스는 네이버가 자주 바꾸지만
n.news.naver.com 기사 URL 패턴 자체는 비교적 안정적이라, 클래스 이름에 의존하는 대신
페이지의 모든 링크를 URL 패턴으로 걸러낸다. 제목/본문/언론사/날짜는 여기서 안 뽑고
NaverArticleScraper가 각 기사 페이지에서 따로 가져온다(그쪽이 더 안정적).
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT_SECONDS = 10.0
ARTICLE_URL = re.compile(r"^https://n\.news\.naver\.com/(mnews/)?article/\d+/\d+")
RESULTS_PER_PAGE = 10
MAX_PAGES = 6


class NaverSearchScraper:
    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def search_article_urls(self, query: str, limit: int) -> list[str]:
        """
        sort=1(최신순)로 검색해서 상위 limit개 기사 URL을 반환한다. 검색결과 한 페이지(10건)
        중 네이버 자체 호스팅(n.news.naver.com) 기사는 절반 이하인 경우가 많아서, limit을
        채울 때까지(또는 MAX_PAGES까지) 페이지를 넘기며 모은다.
        """
        urls: dict[str, None] = {}  # 삽입 순서 유지 + 중복 제거
        for page in range(MAX_PAGES):
            if len(urls) >= limit:
                break
            for url in self._fetch_page(query, page * RESULTS_PER_PAGE + 1):
                urls.setdefault(url, None)
        return list(urls)[:limit]

    def _fetch_page(self, query: str, start: int) -> list[str]:
        try:
            response = self._client.get(
                "https://search.naver.com/search.naver",
                params={"where": "news", "query": query, "sort": "1", "start": start},
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
        except (httpx.HTTPError, ValueError):
            return []

        return [
            normalized
            for normalized in (_strip_query(a["href"]) for a in soup.select("a[href]"))
            if ARTICLE_URL.match(normalized)
        ]


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]
