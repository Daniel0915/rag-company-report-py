"""도구 실행기. 인자 검증, 호출 한도, 반복 차단, 호출 기록을 맡는다."""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.tools import ToolResult, ToolSpec
from app.agent.trace import ToolCallRecord, append_record

# 모델이 낸 인자 문자열이 JSON으로 해석되지 않을 때 원문을 이 키로 감싸 실행기에 넘긴다.
RAW_ARGUMENTS_KEY = "_raw"

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
}


def _validate_value(name: str, value, schema: dict) -> str | None:
    expected = schema.get("type")
    if expected in _TYPE_CHECKS and not _TYPE_CHECKS[expected](value):
        return f"{name}: {expected} 타입이어야 합니다"
    if "enum" in schema and value not in schema["enum"]:
        return f"{name}: {schema['enum']} 중 하나여야 합니다"
    if expected in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{name}: {schema['minimum']} 이상이어야 합니다"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{name}: {schema['maximum']} 이하여야 합니다"
    if expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            error = _validate_value(f"{name}[{i}]", item, schema["items"])
            if error:
                return error
    return None


def validate_arguments(schema: dict, arguments) -> str | None:
    """required·type·enum·minimum/maximum·additionalProperties만 검사한다. 문제가 없으면 None."""
    if not isinstance(arguments, dict):
        return "인자는 객체여야 합니다"
    if set(arguments) == {RAW_ARGUMENTS_KEY} and RAW_ARGUMENTS_KEY not in schema.get("properties", {}):
        return "인자가 올바른 JSON 객체가 아닙니다(JSON 형식 오류). 형식을 맞춰 다시 호출하세요"
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in arguments:
            return f"필수 인자 누락: {key}"
    for key, value in arguments.items():
        if key not in properties:
            if schema.get("additionalProperties") is False:
                return f"알 수 없는 인자: {key}"
            continue
        error = _validate_value(key, value, properties[key])
        if error:
            return error
    return None


class ToolExecutor:
    def __init__(
        self, specs: list[ToolSpec], max_calls: int, trace_path: str | Path | None = None
    ) -> None:
        self._specs = {s.name: s for s in specs}
        self._max_calls = max_calls
        self._trace_path = trace_path
        self._executed = 0
        self._seen: set[tuple[str, str]] = set()
        self.records: list[ToolCallRecord] = []

    @property
    def executed(self) -> int:
        """실제로 실행된 도구 호출 수(거부된 호출은 세지 않는다)."""
        return self._executed

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def execute(self, name: str, arguments: dict) -> ToolResult:
        result = self._run(name, arguments)
        record = ToolCallRecord(
            seq=len(self.records) + 1,
            name=name,
            arguments=arguments if isinstance(arguments, dict) else {RAW_ARGUMENTS_KEY: repr(arguments)},
            ok=result.ok,
            result_text=result.text,
            sources=result.sources,
        )
        self.records.append(record)
        if self._trace_path is not None:
            append_record(self._trace_path, record)
        return result

    def _run(self, name: str, arguments: dict) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(False, f"알 수 없는 도구: {name}")
        error = validate_arguments(spec.input_schema, arguments)
        if error:
            return ToolResult(False, f"인자 오류: {error}")
        key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        if key in self._seen:
            return ToolResult(
                False,
                "같은 호출을 반복했습니다. 다른 도구나 인자를 쓰거나 지금까지의 근거로 답하세요",
            )
        if self._executed >= self._max_calls:
            return ToolResult(
                False,
                f"도구 호출 한도({self._max_calls}회)를 넘었습니다. 지금까지의 근거로 답하세요",
            )
        self._seen.add(key)
        self._executed += 1
        try:
            return spec.run(arguments)
        except Exception as e:  # noqa: BLE001 - 메시지에 키가 섞일 수 있어 클래스명만 노출한다
            return ToolResult(False, f"도구 오류: {type(e).__name__}")
