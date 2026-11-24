"""Tavily 웹검색 API 클라이언트. 키는 설정(.env)에서만 읽고 어디에도 기록하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    snippet: str


class TavilyError(RuntimeError):
    pass


class TavilyClient:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.tavily_api_key
        self._client = httpx.Client(
            base_url=settings.tavily_base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )

    def close(self) -> None:
        self._client.close()

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        if not self._api_key:
            raise TavilyError("TAVILY_API_KEY가 설정되어 있지 않습니다")
        try:
            response = self._client.post(
                "/search",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query, "max_results": max_results, "search_depth": "basic"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            # 응답 본문·헤더·키가 메시지로 새지 않도록 상태 코드만 남긴다.
            raise TavilyError(f"Tavily 검색 실패 (HTTP {e.response.status_code})") from None
        except httpx.HTTPError:
            raise TavilyError("Tavily 검색 요청 실패 (네트워크 오류)") from None
        return [
            WebResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
            )
            for item in data.get("results", [])
        ]
