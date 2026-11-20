import json

import httpx

from app.config import Settings
from app.llm.ollama import OllamaClient, OllamaReply


def _client(options, response_json, captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=response_json)

    client = OllamaClient(Settings(), options=options)
    client._client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return client


def test_chat_sends_options_and_returns_prompt_eval_count():
    captured: dict = {}
    client = _client(
        {"temperature": 0, "seed": 42},
        {"message": {"content": "답"}, "prompt_eval_count": 2050},
        captured,
    )
    reply = client.chat("sys", "질문")
    assert reply == OllamaReply(content="답", prompt_eval_count=2050)
    assert captured["options"] == {"temperature": 0, "seed": 42}


def test_generate_without_options_sends_no_options_key():
    captured: dict = {}
    client = _client(None, {"message": {"content": "답"}}, captured)
    assert client.generate("sys", "질문") == "답"
    assert "options" not in captured


def test_embed_never_sends_options():
    captured: dict = {}
    client = _client({"temperature": 0}, {"embeddings": [[0.1]]}, captured)
    assert client.embed(["a"]) == [[0.1]]
    assert "options" not in captured
