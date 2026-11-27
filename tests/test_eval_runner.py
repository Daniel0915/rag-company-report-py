from datetime import datetime

from app.eval import cli
from app.eval.dataset import Item, Section
from app.eval.judge import JudgeResult
from app.eval.runner import (
    build_run_file,
    format_summary,
    index_snapshot,
    run,
    run_file_name,
    run_item,
    to_retrieved,
)
from app.services.company_chat import Answer
from app.vector.document import Document


def _doc(order="3") -> Document:
    return Document(
        text="매출액은 300조 8,709억원",
        metadata={"rcept_no": "1", "source_file": "a.xml", "section_order": order, "section_title": "개요"},
        score=0.9,
    )


class FakeChat:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)

    def retrieve(self, question, corp_code, top_k, news_query=None):
        if question in self.fail_ids:
            raise TimeoutError("검색 지연")
        return [_doc()]

    def answer(self, question, corp_code, context, provider, search_query=None):
        return Answer(text="매출액은 300조 8,709억원입니다.", prompt_tokens=2050)


def _item(id="q1", ty="A", answerable=True, question="매출액은?") -> Item:
    if not answerable:
        return Item(id=id, corp_code="00126380", question=question, answerable=False, origin="manual")
    return Item(
        id=id,
        corp_code="00126380",
        question=question,
        answerable=True,
        origin="generated",
        expected_answer="300조",
        must_include=["300조"],
        expected_sections=[Section("1", "a.xml", 3, "개요")],
        pblntf_ty=ty,
    )


def test_to_retrieved_coerces_section_order_to_int():
    r = to_retrieved(_doc("3"))
    assert r.section_order == 3 and r.key == ("1", "a.xml", 3) and r.score == 0.9


def test_run_item_scores_answerable_item():
    row = run_item(FakeChat(), _item(), 6, "local", None)
    assert row["metrics"]["hit_strict"] == 1 and row["prompt_tokens"] == 2050 and row["error"] is None
    assert row["retrieved"][0] == {
        "rcept_no": "1", "source_file": "a.xml", "section_order": 3, "section_title": "개요", "score": 0.9,
    }
    assert row["judge"] is None and row["pblntf_ty"] == "A"


def test_run_item_stores_judge_result_as_dict():
    judged = JudgeResult(faithfulness=0.8, correct=True, reason="ok")
    row = run_item(FakeChat(), _item(), 6, "local", lambda item, retrieved, answer: judged)
    assert row["judge"] == {"faithfulness": 0.8, "correct": True, "reason": "ok"}


def test_errored_item_is_excluded_from_denominators():
    rows = run([_item(), _item("q2", question="느린 질문")], FakeChat(fail_ids={"느린 질문"}), 6, "local", None)
    assert rows[1]["error"].startswith("TimeoutError") and rows[1]["metrics"] is None
    assert build_run_file({}, {}, rows)["summary"]["overall"]["hit_strict"]["n"] == 1


def test_answer_failure_keeps_retrieved_and_marks_stage():
    class AnswerTimeout(FakeChat):
        def answer(self, *a, **k):
            raise TimeoutError("생성 지연")

    row = run_item(AnswerTimeout(), _item(), 6, "local", None)
    assert row["retrieved"] and row["error_stage"] == "answer" and row["metrics"] is None


def test_retrieve_failure_marks_stage_with_empty_retrieved():
    row = run_item(FakeChat(fail_ids={"매출액은?"}), _item(), 6, "local", None)
    assert row["retrieved"] == [] and row["error_stage"] == "retrieve" and row["metrics"] is None


def test_successful_item_has_no_error_stage():
    assert run_item(FakeChat(), _item(), 6, "local", None)["error_stage"] is None


def test_run_reports_progress():
    calls = []
    run([_item(), _item("q2")], FakeChat(), 6, "local", None, on_progress=lambda i, n, id: calls.append((i, n, id)))
    assert calls == [(1, 2, "q1"), (2, 2, "q2")]


def test_summary_by_type_groups_unanswerable_as_none_type():
    rows = run([_item("q1", "A"), _item("q2", "I"), _item("m1", answerable=False)], FakeChat(), 6, "local", None)
    run_file = build_run_file({"provider": "local"}, {}, rows)
    assert set(run_file["summary"]["by_type"]) == {"A", "I", "없음"}
    text = format_summary(run_file)
    assert "hit_strict" in text and "[A]" in text and "[없음]" in text


def test_run_file_name():
    assert run_file_name(datetime(2026, 11, 20, 15, 30), "local", 6) == "20261120-153000_local_k6.json"


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        assert "CompanyReportChunk_00126380" in query
        return [{"chunks": 120, "latest": "20260315"}]


class _Driver:
    def session(self):
        return _Session()


def test_index_snapshot_counts_chunks_per_company():
    assert index_snapshot(_Driver(), {"00126380"}) == {"00126380": {"chunks": 120, "latest_rcept_dt": "20260315"}}


def test_run_rejects_empty_dataset(tmp_path, capsys, monkeypatch):
    path = tmp_path / "dataset.yaml"
    path.write_text("[]\n", encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("Neo4j 연결을 시도하면 안 됩니다")

    monkeypatch.setattr(cli.GraphDatabase, "driver", boom)
    assert cli.main(["run", "--dataset", str(path)]) == 1
    assert "정답셋이 비어 있습니다" in capsys.readouterr().out


def test_run_item_tolerates_news_chunk_in_top_k():
    news = Document(
        text="삼성전자 뉴스 본문",
        metadata={"doc_type": "NEWS", "article_url": "http://x", "section_title": None},
        score=0.5,
    )

    class MixedChat(FakeChat):
        def retrieve(self, question, corp_code, top_k, news_query=None):
            return [news, _doc()]

    row = run_item(MixedChat(), _item(), 6, "local", None)
    assert row["error"] is None and row["metrics"]["hit_strict"] == 1
    assert row["retrieved"][0]["section_order"] is None


def test_judge_failure_lines_count_unjudged_rows_and_show_first_error():
    rows = [
        {"metrics": {"hit_strict": 1}, "judge": None},
        {"metrics": {"hit_strict": 1}, "judge": {"faithfulness": 1.0, "correct": True, "reason": ""}},
        {"metrics": None, "judge": None, "error": "TimeoutError: x"},
    ]
    assert cli.judge_failure_lines(rows, "RuntimeError: login") == ["채점 실패 1건", "첫 채점 오류: RuntimeError: login"]
    assert cli.judge_failure_lines(rows[1:2], None) == []


class FakeAgent:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def run(self, question, corp_code, provider):
        from app.agent.service import AgentResult
        from app.agent.trace import ToolCallRecord

        self.calls.append((question, corp_code, provider))
        if self.fail:
            raise RuntimeError("루프 실패")
        chunk = {"kind": "chunk", "corp_name": "삼성", "report_nm": "보고서", "rcept_no": "1",
                 "source_file": "a.xml", "section_order": 3, "section_title": "개요", "doc_type": None,
                 "score": 0.9, "text": "매출액은 300조 8,709억원"}
        rec = ToolCallRecord(1, "search_disclosures", {"query": "매출"}, True, "매출액 300조 8,709억원", [chunk])
        return AgentResult("매출액은 300조 8,709억원입니다.", [rec], 1234)


def test_run_agent_item_row_shape():
    from app.eval.runner import run_agent_item

    agent = FakeAgent()
    seen = []
    row = run_agent_item(
        agent, _item(), "local",
        lambda item, retrieved, answer, evidence_texts=None: seen.append((retrieved, evidence_texts)),
    )
    assert row["tool_calls"] == [{"seq": 1, "name": "search_disclosures", "arguments": {"query": "매출"}, "ok": True}]
    assert row["metrics"]["tool_calls"] == 1 and row["metrics"]["hit_strict"] == 1
    assert row["metrics"]["unsupported_numbers"] == 0
    assert row["prompt_tokens"] == 1234 and row["error"] is None and row["error_stage"] is None
    assert row["retrieved"][0]["section_order"] == 3
    assert seen[0][0][0].section_order == 3
    assert seen[0][1] == ["매출액 300조 8,709억원"]
    assert agent.calls == [("매출액은?", "00126380", "local")]


def test_run_agent_item_error_stages():
    from app.eval.runner import run_agent_item

    row = run_agent_item(FakeAgent(fail=True), _item(), "local", None)
    assert row["metrics"] is None and row["error_stage"] == "agent" and row["tool_calls"] == []

    def bad_judge(item, retrieved, answer, evidence_texts=None):
        raise ValueError("판정 실패")

    row = run_agent_item(FakeAgent(), _item(), "local", bad_judge)
    assert row["error_stage"] == "score" and row["metrics"] is None


def test_run_file_name_agent_marker():
    started = datetime(2026, 11, 24, 15, 30, 0)
    assert run_file_name(started, "local", 6) == "20261124-153000_local_k6.json"
    assert run_file_name(started, "local", 6, agent=True) == "20261124-153000_local_k6_agent.json"


def _cli_json(output_tokens: int) -> str:
    import json

    return json.dumps(
        {"result": "답", "is_error": False, "usage": {"input_tokens": 10, "output_tokens": output_tokens}, "total_cost_usd": 0.5}
    )


def test_run_item_records_answer_usage_but_not_judge_usage():
    from app.llm.claude_cli import parse_cli_json

    class ClaudeChat(FakeChat):
        def answer(self, *a, **k):
            parse_cli_json(_cli_json(5))
            return super().answer(*a, **k)

    def judge_fn(item, retrieved, answer):
        parse_cli_json(_cli_json(999))
        return JudgeResult(faithfulness=1.0, correct=True, reason="ok")

    row = run_item(ClaudeChat(), _item(), 6, "claude", judge_fn)
    assert row["usage"] == {"calls": 1, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5}


def test_local_run_item_has_no_usage():
    assert run_item(FakeChat(), _item(), 6, "local", None)["usage"] is None


def test_run_file_name_marks_external_mcp():
    name = run_file_name(datetime(2026, 11, 27, 9, 0), "deepseek", 6, agent=True, external=True)
    assert name == "20261127-090000_deepseek_k6_agent_ext.json"


def test_cli_external_mcp_flag_sets_settings():
    from app.config import Settings
    from app.eval.cli import build_parser, run_settings

    base = Settings(_env_file=None)
    on = run_settings(build_parser().parse_args(["run", "--agent", "--external-mcp"]), base)
    off = run_settings(build_parser().parse_args(["run", "--agent"]), base)
    assert on.agent_external_mcp is True and off.agent_external_mcp is False
