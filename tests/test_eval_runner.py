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
