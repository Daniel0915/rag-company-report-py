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

    def step(self, messages, tools):
        self.seen.append(list(messages))
        self.tools_seen.append(tools)
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


def test_loop_forces_final_answer_when_model_never_stops():
    def call(i):
        return ModelStep("", [ToolCall("calculate", {"expression": f"{i}+1"})])

    model = Scripted([call(i) for i in range(4)] + [ModelStep("강제 답", [])])
    result = run_tool_loop(model, _executor(max_calls=2), "sys", "q")
    assert result.answer == "강제 답"
    assert len(model.seen) == 5  # max_calls(2)+2 번의 step + 마지막 강제 step
    assert model.tools_seen[-1] == []
    assert model.seen[-1][-1]["role"] == "user"
    assert all(t for t in model.tools_seen[:-1])


def test_any_step_model_works():
    class Minimal:  # StepModel 프로토콜만 구현
        def step(self, messages, tools):
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
            ToolCall("calculate", {}),
        ],
        prompt_tokens=900,
    )
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "web_search"
    assert captured["tools"][0]["function"]["parameters"] == spec.input_schema
