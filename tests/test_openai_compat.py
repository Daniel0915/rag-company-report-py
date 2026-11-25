import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion

from app.agent.executor import ToolExecutor
from app.agent.loop import FINAL_ANSWER_PROMPT, OpenAIStepModel, run_tool_loop
from app.agent.tools import ToolResult, ToolSpec
from app.config import Settings
from app.llm.openai_compat import OpenAICompatClient
from app.llm.usage import metered


def _completion(content=None, tool_calls=None, cost=0.0001) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek/deepseek-v4.1-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                }
            ],
            "usage": {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340, "cost": cost},
        }
    )


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))
        return self.responses.pop(0)

    def close(self):
        pass


def _client(fake) -> OpenAICompatClient:
    return OpenAICompatClient(Settings(_env_file=None), client=fake)


def test_generate_returns_text_and_records_usage():
    fake = FakeOpenAI([_completion(content=" 답 ")])
    with metered() as calls:
        assert _client(fake).generate("시스템", "질문") == "답"
    assert fake.requests[0]["model"] == "deepseek/deepseek-v4.1-flash"
    assert fake.requests[0]["messages"][0] == {"role": "system", "content": "시스템"}
    [call] = calls
    assert (call.input_tokens, call.output_tokens, call.cost_usd) == (300, 40, 0.0001)


def test_missing_key_fails_on_call_not_on_startup():
    client = OpenAICompatClient(Settings(_env_file=None, openrouter_api_key=""))
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        client.generate("", "질문")
    client.close()


def _echo_tool() -> ToolSpec:
    return ToolSpec(
        name="search_disclosures",
        description="공시 검색",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        run=lambda args: ToolResult(True, f"결과:{args['query']}", []),
    )


def test_tool_loop_round_trip_keeps_call_ids_and_openai_shape():
    call = {"id": "call_1", "type": "function", "function": {"name": "search_disclosures", "arguments": '{"query": "매출"}'}}
    fake = FakeOpenAI([_completion(tool_calls=[call]), _completion(content="매출은 10조입니다.")])
    executor = ToolExecutor([_echo_tool()], max_calls=6)

    result = run_tool_loop(OpenAIStepModel(_client(fake)), executor, "시스템", "매출은?")

    assert result.answer == "매출은 10조입니다."
    assert [r.arguments for r in executor.records] == [{"query": "매출"}]
    second = fake.requests[1]["messages"]
    assert second[2]["role"] == "assistant" and second[2]["tool_calls"][0]["id"] == "call_1"
    assert second[3] == {"role": "tool", "content": "결과:매출", "tool_call_id": "call_1"}
    assert fake.requests[0]["tools"][0]["function"]["name"] == "search_disclosures"
    assert "tool_choice" not in fake.requests[0]


def test_forced_final_turn_sends_tool_choice_none():
    call = {"id": "c", "type": "function", "function": {"name": "search_disclosures", "arguments": '{"query": "a"}'}}
    fake = FakeOpenAI([_completion(tool_calls=[call]), _completion(content="최종")])
    executor = ToolExecutor([_echo_tool()], max_calls=1)

    assert run_tool_loop(OpenAIStepModel(_client(fake)), executor, "s", "q").answer == "최종"
    last = fake.requests[-1]
    assert last["tool_choice"] == "none"
    assert last["messages"][-1] == {"role": "user", "content": FINAL_ANSWER_PROMPT}


def test_malformed_arguments_are_kept_for_the_executor_to_reject():
    call = {"id": "c", "type": "function", "function": {"name": "search_disclosures", "arguments": "{broken"}}
    fake = FakeOpenAI([_completion(tool_calls=[call]), _completion(content="끝")])
    executor = ToolExecutor([_echo_tool()], max_calls=6)

    run_tool_loop(OpenAIStepModel(_client(fake)), executor, "s", "q")
    assert executor.records[0].ok is False
