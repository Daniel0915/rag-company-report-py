"""로컬 Ollama 채팅 모델 + 임베딩 모델 클라이언트."""
from __future__ import annotations

import httpx

from app.config import Settings


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._chat_model = settings.ollama_chat_model
        self._embedding_model = settings.ollama_embedding_model
        # 로컬 모델은 첫 응답까지 오래 걸릴 수 있어 read timeout을 넉넉히 준다.
        self._client = httpx.Client(
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=5.0),
        )

    def close(self) -> None:
        self._client.close()

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        messages = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        response = self._client.post(
            "/api/chat",
            json={"model": self._chat_model, "messages": messages, "stream": False},
        )
        response.raise_for_status()
        return (response.json().get("message") or {}).get("content", "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """여러 텍스트를 한 번에 임베딩한다. 반환 순서는 입력 순서와 같다."""
        if not texts:
            return []
        response = self._client.post(
            "/api/embed", json={"model": self._embedding_model, "input": texts}
        )
        response.raise_for_status()
        embeddings = response.json().get("embeddings")
        if not embeddings:
            raise RuntimeError("Ollama 임베딩 응답이 비어 있습니다")
        return embeddings

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
