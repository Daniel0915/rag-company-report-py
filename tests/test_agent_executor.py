from app.agent.executor import ToolExecutor
from app.agent.tools import ToolSpec, build_tools
from app.agent.trace import ToolCallRecord, append_record, read_records
from test_agent_tools import fake_ctx


def test_executor_runs_tool():
    r = ToolExecutor(build_tools(fake_ctx()), max_calls=6).execute("calculate", {"expression": "1+1"})
    assert r.ok and r.text == "2"


def test_executor_unknown_tool():
    r = ToolExecutor(build_tools(fake_ctx()), max_calls=6).execute("nope", {})
    assert not r.ok and "알 수 없는 도구" in r.text


def test_executor_returns_error_for_bad_arguments():
    ex = ToolExecutor(build_tools(fake_ctx()), max_calls=6)
    assert "인자 오류" in ex.execute("calculate", {}).text
    assert not ex.execute("calculate", {"expression": 1}).ok
    assert not ex.execute("calculate", {"expression": "1", "x": 2}).ok
    assert not ex.execute("search_disclosures", {"query": "a", "doc_type": "ETC"}).ok
    assert not ex.execute("search_disclosures", {"query": "a", "top_k": 11}).ok
    assert not ex.execute("search_disclosures", {"query": "a", "top_k": True}).ok
    assert not ex.execute("list_recent_disclosures", {"types": ["Z"]}).ok
    assert not ex.execute("calculate", []).ok


def test_executor_blocks_repeat_and_enforces_limit(tmp_path):
    ex = ToolExecutor(build_tools(fake_ctx()), max_calls=2, trace_path=tmp_path / "t.jsonl")
    ex.execute("calculate", {"expression": "1+1"})
    r = ex.execute("calculate", {"expression": "1+1"})
    assert not r.ok and "반복" in r.text
    ex.execute("calculate", {"expression": "2+2"})
    r = ex.execute("calculate", {"expression": "3+3"})
    assert not r.ok and "한도(2회)" in r.text
    assert [rec.seq for rec in read_records(tmp_path / "t.jsonl")] == [1, 2, 3, 4]
    assert [rec.ok for rec in ex.records] == [True, False, True, False]


def test_rejected_calls_do_not_consume_budget():
    ex = ToolExecutor(build_tools(fake_ctx()), max_calls=1)
    ex.execute("calculate", {})
    ex.execute("nope", {})
    assert ex.execute("calculate", {"expression": "1+1"}).ok


def test_executor_hides_exception_message():
    def boom(args):
        raise RuntimeError("secret tvly-xxx")

    spec = ToolSpec("boom", "d", {"type": "object", "properties": {}, "additionalProperties": False}, boom)
    r = ToolExecutor([spec], max_calls=3).execute("boom", {})
    assert not r.ok and "tvly" not in r.text and "RuntimeError" in r.text


def test_trace_roundtrip_and_missing_file(tmp_path):
    assert read_records(tmp_path / "none.jsonl") == []
    rec = ToolCallRecord(1, "web_search", {"query": "삼성"}, True, "결과", [{"kind": "web"}])
    append_record(tmp_path / "a.jsonl", rec)
    assert "삼성" in (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert read_records(tmp_path / "a.jsonl") == [rec]


def test_validate_supports_boolean_object_number_and_ignores_unknown_types():
    from app.agent.executor import validate_arguments

    schema = {
        "properties": {
            "flag": {"type": "boolean"},
            "obj": {"type": "object"},
            "num": {"type": "number"},
            "weird": {"type": "null"},
        }
    }
    assert validate_arguments(schema, {"flag": True, "obj": {}, "num": 1.5, "weird": 3}) is None
    assert validate_arguments(schema, {"flag": 1}) is not None
    assert validate_arguments(schema, {"obj": []}) is not None
    assert validate_arguments(schema, {"num": True}) is not None


def test_execute_reports_malformed_json_arguments():
    executor = ToolExecutor(build_tools(fake_ctx()), max_calls=6)
    result = executor.execute("calculate", {"_raw": "{깨짐"})
    assert not result.ok and "JSON" in result.text
