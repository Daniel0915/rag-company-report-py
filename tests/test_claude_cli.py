import json
import subprocess

import pytest

from app.config import Settings
from app.llm.claude_cli import ClaudeCliClient, parse_cli_json
from app.llm.usage import metered

_PAYLOAD = {
    "result": " 답 ",
    "is_error": False,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 600,
        "cache_read_input_tokens": 30,
        "output_tokens": 7,
    },
    "total_cost_usd": 0.0001,
}


def test_parse_cli_json_reads_answer_and_usage():
    with metered() as calls:
        assert parse_cli_json(json.dumps(_PAYLOAD)) == "답"
    [call] = calls
    assert call.input_tokens == 632
    assert call.output_tokens == 7
    assert call.cost_usd == 0.0001


def test_parse_cli_json_errors():
    with pytest.raises(RuntimeError):
        parse_cli_json("not json")
    with pytest.raises(RuntimeError):
        parse_cli_json('{"result": "boom", "is_error": true}')


def test_metered_collects_only_inside_block():
    parse_cli_json(json.dumps(_PAYLOAD))
    with metered() as calls:
        parse_cli_json(json.dumps(_PAYLOAD))
        parse_cli_json(json.dumps(_PAYLOAD))
    parse_cli_json(json.dumps(_PAYLOAD))
    assert [c.output_tokens for c in calls] == [7, 7]


def test_client_isolates_cli_and_records_usage(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_PAYLOAD), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = ClaudeCliClient(Settings(_env_file=None, claude_model="haiku"))
    with metered() as calls:
        assert client.generate("시스템", "질문") == "답"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert len(calls) == 1
