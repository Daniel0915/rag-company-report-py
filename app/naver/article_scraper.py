"""
네이버뉴스 기사 페이지(n.news.naver.com)에서 본문을 크롤링한다. 기사 페이지는
서버사이드 렌더링이라 브라우저 자동화 없이 정적 HTML 파싱만으로 충분하다.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT_SECONDS = 10.0

CONTENT_SELECTORS = ["#dic_area", "article#dic_area", ".go_trans._article_content", "._article_body_contents"]
TITLE_SELECTORS = [
    "#title_area span",
    "#ct .media_end_head_headline",
    ".media_end_head_headline",
    "h2#title_area",
    ".news_end_title",
]


@dataclass(frozen=True)
class ScrapedArticle:
    """press/published_date는 못 찾으면 None일 수 있다."""

    title: str | None
    content: str
    press: str | None
    published_date: str | None


class NaverArticleScraper:
    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def fetch_article(self, url: str) -> ScrapedArticle | None:
        """구조가 안 맞거나 네트워크 오류면 None - 호출부가 실패로 기록하고 넘어간다."""
        try:
            response = self._client.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
        except (httpx.HTTPError, ValueError):
            return None

        content = _first_non_blank_text(soup, CONTENT_SELECTORS)
        if not content:
            return None

        return ScrapedArticle(
            title=_first_non_blank_text(soup, TITLE_SELECTORS),
            content=content,
            press=_extract_press(soup),
            published_date=_extract_published_date(soup),
        )


def _first_non_blank_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = element.get_text(" ", strip=True)
            if text:
                return text
    return None


def _extract_press(soup: BeautifulSoup) -> str | None:
    logo_img = soup.select_one("a.media_end_head_top_logo img")
    if logo_img and logo_img.get("alt", "").strip():
        return logo_img["alt"].strip()
    logo_text = soup.select_one(".media_end_head_top_logo_text")
    return logo_text.get_text(strip=True) if logo_text else None


def _extract_published_date(soup: BeautifulSoup) -> str | None:
    element = soup.select_one("span.media_end_head_info_datestamp_time, span[data-date-time]")
    if element is None:
        return None
    data_date_time = (element.get("data-date-time") or "").strip()
    return data_date_time or element.get_text(strip=True)
