"""로컬 Ollama 채팅 모델 + 임베딩 모델 클라이언트."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings


@dataclass(frozen=True)
class OllamaReply:
    content: str
    # 입력(프롬프트)이 차지한 토큰 수. Ollama가 응답에 안 넣으면 None.
    prompt_eval_count: int | None


class OllamaClient:
    def __init__(self, settings: Settings, options: dict | None = None) -> None:
        # /api/chat 요청에만 붙는 생성 옵션(temperature, seed 등). 임베딩에는 쓰지 않는다.
        self._options = options
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
        return self.chat(system_prompt, user_prompt).content

    def chat(self, system_prompt: str, user_prompt: str) -> OllamaReply:
        messages = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        body: dict = {"model": self._chat_model, "messages": messages, "stream": False}
        if self._options is not None:
            body["options"] = self._options
        response = self._client.post("/api/chat", json=body)
        response.raise_for_status()
        data = response.json()
        return OllamaReply(
            content=(data.get("message") or {}).get("content", ""),
            prompt_eval_count=data.get("prompt_eval_count"),
        )

    def chat_messages(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """메시지 목록 그대로 /api/chat을 부르고 응답 JSON을 돌려준다(도구 호출 루프용)."""
        body: dict = {"model": self._chat_model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        if self._options is not None:
            body["options"] = self._options
        response = self._client.post("/api/chat", json=body)
        response.raise_for_status()
        return response.json()

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
