"""Google Gemini(generateContent) REST API를 직접 호출하는 클라이언트."""
from __future__ import annotations

import httpx

from app.config import Settings


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.gemini_api_key
        self._model = settings.gemini_model
        self._client = httpx.Client(
            base_url=settings.gemini_base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
        )

    def close(self) -> None:
        self._client.close()

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if not self._api_key.strip():
            raise RuntimeError("GEMINI_API_KEY가 설정되어 있지 않습니다")

        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        }
        response = self._client.post(
            f"/models/{self._model}:generateContent",
            params={"key": self._api_key},
            json=body,
        )
        response.raise_for_status()

        candidates = response.json().get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini 응답이 비어 있습니다")
        return candidates[0]["content"]["parts"][0]["text"]
