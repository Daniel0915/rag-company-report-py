"""OpenAI 호환 API 클라이언트. 지금은 OpenRouter를 거쳐 DeepSeek를 부른다(선불 크레딧, API 키는 .env에만)."""
from __future__ import annotations

from openai import OpenAI
from openai.types.chat import ChatCompletion

from app.config import Settings
from app.llm.usage import LlmCall, record


class OpenAICompatClient:
    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        self._model = settings.deepseek_model
        self._has_key = bool(settings.openrouter_api_key) or client is not None
        self._client = client or OpenAI(
            # 키가 없을 때도 앱이 뜨도록 자리값을 넣고, 실제 호출에서 막는다
            api_key=settings.openrouter_api_key or "unset",
            base_url=settings.openrouter_base_url,
            timeout=settings.deepseek_timeout_seconds,
            max_retries=2,
        )

    @property
    def model(self) -> str:
        return self._model

    def complete(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str | None = None) -> ChatCompletion:
        """chat.completions 한 번. 사용량을 기록한다."""
        if not self._has_key:
            raise RuntimeError("OPENROUTER_API_KEY가 설정되지 않았습니다")
        kwargs: dict = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        response = self._client.chat.completions.create(**kwargs)
        usage = response.usage
        if usage is not None:
            record(
                LlmCall(
                    input_tokens=usage.prompt_tokens or 0,
                    output_tokens=usage.completion_tokens or 0,
                    cost_usd=float(getattr(usage, "cost", None) or 0.0),  # OpenRouter가 붙이는 비용
                )
            )
        if not response.choices:
            raise RuntimeError("DeepSeek 응답에 choices가 없습니다")
        return response

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        messages = [{"role": "user", "content": user_prompt}]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        return (self.complete(messages).choices[0].message.content or "").strip()

    def close(self) -> None:
        self._client.close()
