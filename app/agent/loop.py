"""모델과 무관한 도구 호출 루프. 모델은 StepModel만 구현하면 된다(Ollama, OpenAI 호환 API 어댑터)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.executor import RAW_ARGUMENTS_KEY, ToolExecutor
from app.agent.tools import ToolSpec
from app.llm.ollama import OllamaClient
from app.llm.openai_compat import OpenAICompatClient

FINAL_ANSWER_PROMPT = "도구는 더 쓸 수 없습니다. 지금까지의 근거로 최종 답변을 하세요."


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict
    id: str | None = None  # 제공자가 붙인 호출 id(Claude API 등). 있으면 도구 결과 메시지에 실린다


@dataclass(frozen=True)
class ModelStep:
    content: str
    tool_calls: list[ToolCall]
    prompt_tokens: int | None = None
    # 제공자의 어시스턴트 메시지 원문. 있으면 루프가 content/tool_calls로 다시 만들지 않고 그대로 붙인다.
    raw_assistant: Any = None


class StepModel(Protocol):
    """모델 한 번 호출. 루프가 쌓는 messages 규약은 다음과 같다.

    - 처음에 system, user 메시지 하나씩.
    - 도구 호출이 있는 step마다 어시스턴트 메시지 하나(raw_assistant이 있으면 그대로,
      없으면 content와 tool_calls로 만든 {"role": "assistant", ...}), 이어서 호출 순서대로
      {"role": "tool", "content", "tool_name"} 메시지(호출에 id가 있으면 "tool_call_id"도).
    - 도구 한도에 닿거나 반복 한도가 차면 user 메시지(FINAL_ANSWER_PROMPT)를 붙이고
      allow_tools=False로 마지막 step을 부른다. 이때도 tools는 그대로 넘기므로
      어댑터는 "도구 금지"(예: tool_choice none)와 "도구 없음"을 구분할 수 있다.
    """

    def step(
        self, messages: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> ModelStep: ...


@dataclass(frozen=True)
class LoopResult:
    answer: str
    prompt_tokens: int | None


def _assistant_message(step: ModelStep) -> dict:
    return {
        "role": "assistant",
        "content": step.content,
        "tool_calls": [
            {"function": {"name": c.name, "arguments": c.arguments}} for c in step.tool_calls
        ],
    }


def run_tool_loop(
    model: StepModel, executor: ToolExecutor, system_prompt: str, question: str
) -> LoopResult:
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    tokens: list[int] = []

    def call_model(allow_tools: bool) -> ModelStep:
        step = model.step(messages, executor.specs, allow_tools=allow_tools)
        if step.prompt_tokens is not None:
            tokens.append(step.prompt_tokens)
        return step

    for _ in range(executor.max_calls + 2):
        step = call_model(True)
        if not step.tool_calls:
            return LoopResult(step.content, max(tokens, default=None))
        messages.append(
            step.raw_assistant if step.raw_assistant is not None else _assistant_message(step)
        )
        for call in step.tool_calls:
            result = executor.execute(call.name, call.arguments)
            tool_message = {"role": "tool", "content": result.text, "tool_name": call.name}
            if call.id is not None:
                tool_message["tool_call_id"] = call.id
            messages.append(tool_message)
        if executor.executed >= executor.max_calls:
            break

    messages.append({"role": "user", "content": FINAL_ANSWER_PROMPT})
    step = call_model(False)
    return LoopResult(step.content, max(tokens, default=None))


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {RAW_ARGUMENTS_KEY: str(raw)}
    return parsed if isinstance(parsed, dict) else {RAW_ARGUMENTS_KEY: str(raw)}


class OllamaStepModel:
    """Ollama Function Calling 응답을 ModelStep으로 바꾸는 어댑터."""

    def __init__(self, client: OllamaClient) -> None:
        self._client = client

    def step(
        self, messages: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> ModelStep:
        # Ollama에는 tool_choice가 없어 도구 금지는 tools를 빼서 표현한다
        payload = _function_tools(tools) if allow_tools else []
        data = self._client.chat_messages(messages, payload)
        message = data.get("message") or {}
        calls = [
            ToolCall(c["function"]["name"], _parse_arguments(c["function"].get("arguments")))
            for c in message.get("tool_calls") or []
        ]
        return ModelStep(message.get("content", ""), calls, data.get("prompt_eval_count"))


class OpenAIStepModel:
    """OpenAI 호환 chat.completions(지금은 OpenRouter의 DeepSeek) 응답을 ModelStep으로 바꾸는 어댑터."""

    def __init__(self, client: OpenAICompatClient) -> None:
        self._client = client

    def step(
        self, messages: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> ModelStep:
        response = self._client.complete(
            [_openai_message(m) for m in messages],
            _function_tools(tools),
            tool_choice=None if allow_tools else "none",
        )
        message = response.choices[0].message
        calls = [
            ToolCall(c.function.name, _parse_arguments(c.function.arguments), id=c.id)
            for c in message.tool_calls or []
        ]
        return ModelStep(
            message.content or "",
            calls,
            response.usage.prompt_tokens if response.usage else None,
            # 추론 필드(reasoning 등)까지 그대로 돌려보내야 이어지는 턴에서 맥락이 끊기지 않는다
            raw_assistant=message.model_dump(exclude_none=True),
        )


def _function_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
        }
        for t in tools
    ]


def _openai_message(message: dict) -> dict:
    """루프의 도구 결과 메시지에서 OpenAI 규격에 없는 tool_name을 뺀다."""
    if message.get("role") == "tool":
        return {k: v for k, v in message.items() if k != "tool_name"}
    return message
