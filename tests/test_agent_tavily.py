import json

import httpx
import pytest

from app.agent.tavily import TavilyClient, TavilyError, WebResult
from app.config import Settings


def _client(handler, key="tvly-test"):
    client = TavilyClient(Settings(tavily_api_key=key))
    client._client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return client


def test_tavily_search_parses_results_and_sends_bearer():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["json"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(
            200, json={"results": [{"title": "t", "url": "https://x", "content": "요약"}]}
        )

    assert _client(handler).search("삼성전자 HBM", 3) == [WebResult("t", "https://x", "요약")]
    assert captured["path"] == "/search"
    assert captured["headers"]["authorization"] == "Bearer tvly-test"
    assert captured["json"] == {"query": "삼성전자 HBM", "max_results": 3, "search_depth": "basic"}


def test_tavily_error_message_never_contains_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid key tvly-test")

    with pytest.raises(TavilyError) as e:
        _client(handler).search("q")
    assert "tvly-test" not in str(e.value)
    assert "401" in str(e.value)


def test_tavily_requires_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("키가 없으면 요청을 보내면 안 된다")

    with pytest.raises(TavilyError, match="TAVILY_API_KEY"):
        _client(handler, key="").search("q")
