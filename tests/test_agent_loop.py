import json

import httpx

from app.agent.executor import ToolExecutor
from app.agent.loop import ModelStep, OllamaStepModel, ToolCall, run_tool_loop
from app.agent.tools import build_tools
from app.config import Settings
from app.llm.ollama import OllamaClient
from test_agent_tools import fake_ctx


class Scripted:
    """정해진 ModelStep을 차례로 돌려주고, 받은 messages/tools를 기록하는 가짜 StepModel."""

    def __init__(self, steps):
        self._steps = iter(steps)
        self.seen: list[list[dict]] = []
        self.tools_seen: list[list] = []
        self.allow_seen: list[bool] = []

    def step(self, messages, tools, allow_tools=True):
        self.seen.append(list(messages))
        self.tools_seen.append(tools)
        self.allow_seen.append(allow_tools)
        return next(self._steps)


def _executor(max_calls=6):
    return ToolExecutor(build_tools(fake_ctx()), max_calls=max_calls)


def test_loop_executes_tool_then_returns_answer():
    model = Scripted(
        [ModelStep("", [ToolCall("calculate", {"expression": "1+2"})]), ModelStep("답: 3", [])]
    )
    executor = _executor()
    result = run_tool_loop(model, executor, "sys", "1+2는?")
    assert result.answer == "답: 3"
    assert model.seen[0] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "1+2는?"},
    ]
    assert model.seen[1][-1] == {
        "role": "tool",
        "content": executor.records[-1].result_text,
        "tool_name": "calculate",
    }
    assert model.seen[1][-2]["role"] == "assistant"
    assert model.seen[1][-2]["tool_calls"]


def test_loop_forces_final_answer_as_soon_as_max_calls_executed():
    def call(i):
        return ModelStep("", [ToolCall("calculate", {"expression": f"{i}+1"})])

    model = Scripted([call(0), call(1), ModelStep("강제 답", [])])
    executor = _executor(max_calls=2)
    result = run_tool_loop(model, executor, "sys", "q")
    assert result.answer == "강제 답"
    assert len(model.seen) == 3  # 실행 2회 직후 곧바로 강제 step, 한도 초과 호출 낭비 없음
    assert len(executor.records) == 2
    assert model.seen[-1][-1]["role"] == "user"
    assert model.allow_seen == [True, True, False]
    # 강제 턴도 같은 도구 목록을 그대로 넘긴다(allow_tools로만 구분)
    assert model.tools_seen[-1] == executor.specs and executor.specs


def test_loop_is_bounded_when_model_repeats_without_executing():
    repeat = ModelStep("", [ToolCall("calculate", {"expression": "1+1"})])
    model = Scripted([repeat] * 4 + [ModelStep("마무리", [])])
    result = run_tool_loop(model, _executor(max_calls=2), "sys", "q")
    assert result.answer == "마무리"
    assert model.allow_seen[-1] is False


def test_loop_executes_two_calls_in_one_step_in_order_and_keeps_content():
    model = Scripted(
        [
            ModelStep(
                "먼저 계산합니다",
                [ToolCall("calculate", {"expression": "1+1"}), ToolCall("calculate", {"expression": "2+2"})],
            ),
            ModelStep("끝", []),
        ]
    )
    run_tool_loop(model, _executor(), "sys", "q")
    msgs = model.seen[1]
    assert msgs[-3]["role"] == "assistant" and msgs[-3]["content"] == "먼저 계산합니다"
    assert len(msgs[-3]["tool_calls"]) == 2
    assert [m["content"] for m in msgs[-2:]] == ["2", "4"]
    assert all(m["role"] == "tool" for m in msgs[-2:])


def test_loop_appends_raw_assistant_verbatim_and_tool_call_id():
    raw = {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1"}], "extra": 1}
    model = Scripted(
        [
            ModelStep("", [ToolCall("calculate", {"expression": "1+1"}, id="tu_1")], raw_assistant=raw),
            ModelStep("끝", []),
        ]
    )
    run_tool_loop(model, _executor(), "sys", "q")
    msgs = model.seen[1]
    assert msgs[-2] is raw
    assert msgs[-1]["role"] == "tool" and msgs[-1]["tool_call_id"] == "tu_1"


def test_tool_message_has_no_call_id_when_call_has_none():
    model = Scripted(
        [ModelStep("", [ToolCall("calculate", {"expression": "1+1"})]), ModelStep("끝", [])]
    )
    run_tool_loop(model, _executor(), "sys", "q")
    assert "tool_call_id" not in model.seen[1][-1]


def test_malformed_arguments_reach_executor_as_malformed_json_error():
    executor = _executor()
    model = Scripted(
        [ModelStep("", [ToolCall("calculate", {"_raw": "{깨짐"})]), ModelStep("끝", [])]
    )
    run_tool_loop(model, executor, "sys", "q")
    assert not executor.records[0].ok
    assert "JSON" in executor.records[0].result_text
    assert "필수 인자 누락" not in executor.records[0].result_text


def test_any_step_model_works():
    class Minimal:  # StepModel 프로토콜만 구현
        def step(self, messages, tools, allow_tools=True):
            return ModelStep("바로 답", [], prompt_tokens=7)

    result = run_tool_loop(Minimal(), _executor(), "sys", "q")
    assert (result.answer, result.prompt_tokens) == ("바로 답", 7)


def test_prompt_tokens_is_max_over_steps():
    model = Scripted(
        [
            ModelStep("", [ToolCall("calculate", {"expression": "1+1"})], prompt_tokens=500),
            ModelStep("끝", [], prompt_tokens=900),
        ]
    )
    assert run_tool_loop(model, _executor(), "s", "q").prompt_tokens == 900


def _ollama_model(response_json, captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=response_json)

    client = OllamaClient(Settings())
    client._client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return OllamaStepModel(client)


def test_ollama_step_model_converts_tools_and_calls():
    captured: dict = {}
    model = _ollama_model(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": "{\"query\": \"x\"}"}},
                    {"function": {"name": "calculate", "arguments": {"expression": "1+1"}}},
                    {"function": {"name": "calculate", "arguments": "깨진 json"}},
                ],
            },
            "prompt_eval_count": 900,
        },
        captured,
    )
    spec = next(s for s in build_tools(fake_ctx()) if s.name == "web_search")
    step = model.step([{"role": "user", "content": "q"}], [spec])
    assert step == ModelStep(
        "",
        [
            ToolCall("web_search", {"query": "x"}),
            ToolCall("calculate", {"expression": "1+1"}),
            ToolCall("calculate", {"_raw": "깨진 json"}),
        ],
        prompt_tokens=900,
    )
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "web_search"
    assert captured["tools"][0]["function"]["parameters"] == spec.input_schema


def test_ollama_step_model_omits_tools_when_not_allowed():
    captured: dict = {}
    model = _ollama_model({"message": {"content": "답"}}, captured)
    spec = next(s for s in build_tools(fake_ctx()) if s.name == "web_search")
    step = model.step([{"role": "user", "content": "q"}], [spec], allow_tools=False)
    assert "tools" not in captured
    assert step == ModelStep("답", [], prompt_tokens=None) and step.raw_assistant is None
