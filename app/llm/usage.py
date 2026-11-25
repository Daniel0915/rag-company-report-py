"""외부 LLM 호출(claude CLI, OpenAI 호환 API)의 토큰과 비용을 모은다. 평가가 문항별 사용량을 남기는 데 쓴다."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class LlmCall:
    input_tokens: int  # 캐시 생성/읽기 토큰 포함
    output_tokens: int
    cost_usd: float  # 제공자가 알려준 값. claude CLI는 API 단가로 계산할 뿐 구독에서는 청구되지 않는다


_meter: ContextVar[list[LlmCall] | None] = ContextVar("llm_meter", default=None)


@contextmanager
def metered() -> Iterator[list[LlmCall]]:
    """이 블록 안에서 record()된 호출을 모은다. 블록 밖 호출은 버린다."""
    calls: list[LlmCall] = []
    token = _meter.set(calls)
    try:
        yield calls
    finally:
        _meter.reset(token)


def record(call: LlmCall) -> None:
    calls = _meter.get()
    if calls is not None:
        calls.append(call)
