"""모델과 무관한 도구 호출 루프. 모델은 StepModel만 구현하면 된다(Ollama 어댑터, 이후 Claude API)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from app.agent.executor import ToolExecutor
from app.agent.tools import ToolSpec
from app.llm.ollama import OllamaClient

FINAL_ANSWER_PROMPT = "도구는 더 쓸 수 없습니다. 지금까지의 근거로 최종 답변을 하세요."


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class ModelStep:
    content: str
    tool_calls: list[ToolCall]
    prompt_tokens: int | None = None


class StepModel(Protocol):
    def step(self, messages: list[dict], tools: list[ToolSpec]) -> ModelStep: ...


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

    def call_model(tools: list[ToolSpec]) -> ModelStep:
        step = model.step(messages, tools)
        if step.prompt_tokens is not None:
            tokens.append(step.prompt_tokens)
        return step

    for _ in range(executor.max_calls + 2):
        step = call_model(executor.specs)
        if not step.tool_calls:
            return LoopResult(step.content, max(tokens, default=None))
        messages.append(_assistant_message(step))
        for call in step.tool_calls:
            result = executor.execute(call.name, call.arguments)
            messages.append({"role": "tool", "content": result.text, "tool_name": call.name})

    messages.append({"role": "user", "content": FINAL_ANSWER_PROMPT})
    step = call_model([])
    return LoopResult(step.content, max(tokens, default=None))


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OllamaStepModel:
    """Ollama Function Calling 응답을 ModelStep으로 바꾸는 어댑터."""

    def __init__(self, client: OllamaClient) -> None:
        self._client = client

    def step(self, messages: list[dict], tools: list[ToolSpec]) -> ModelStep:
        payload = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        data = self._client.chat_messages(messages, payload)
        message = data.get("message") or {}
        calls = [
            ToolCall(c["function"]["name"], _parse_arguments(c["function"].get("arguments")))
            for c in message.get("tool_calls") or []
        ]
        return ModelStep(message.get("content", ""), calls, data.get("prompt_eval_count"))
