import pytest

from app.eval.dataset import Item, Section
from app.eval.metrics import (
    Retrieved,
    aggregate,
    aggregate_by_type,
    extract_numbers,
    fact_recall,
    hit_loose,
    hit_strict,
    is_refusal,
    mrr,
    score_item,
    unsupported_numbers,
)


def _ret(rcept_no="R1", source_file="R1.xml", order=3, title="1. 사업의 개요", text=""):
    return Retrieved(rcept_no, source_file, order, title, None, text)


def _exp(rcept_no="R1", source_file="R1.xml", order=3, title="1. 사업의 개요"):
    return Section(rcept_no, source_file, order, title)


def test_hit_strict_needs_source_file():
    assert hit_strict([_ret(source_file="AUDIT.xml")], [_exp()]) == 0
    assert hit_strict([_ret()], [_exp()]) == 1


def test_hit_loose_matches_same_title_in_other_report():
    r = [_ret("R2", "R2.xml", 5)]
    assert hit_strict(r, [_exp()]) == 0
    assert hit_loose(r, [_exp()]) == 1


def test_mrr_uses_first_strict_rank():
    r = [_ret(order=8), _ret(order=9), _ret()]
    assert mrr(r, [_exp()]) == pytest.approx(1 / 3)
    assert mrr([_ret(order=8)], [_exp()]) == 0.0


def test_fact_recall_ignores_spaces_and_commas():
    assert fact_recall("매출액은 300조 8709억원", ["300조 8,709억", "영업이익 6조"]) == 0.5


def test_refusal_detection():
    assert is_refusal("제공된 공시에서는 확인할 수 없습니다.")
    assert not is_refusal("매출액은 300조원입니다.")


def test_refusal_detection_covers_baseline_misses():
    # 첫 기준선(2026-11-21)에서 거절인데 거절로 못 잡은 실제 답변들
    assert is_refusal("2029년에 인수될 예정이라는 정보는 명시되어 있지 않습니다.")
    assert is_refusal("I'm sorry, but the specific details regarding the dividend plan are not provided in the data.")
    assert is_refusal("The provided data does not contain specific information about daily diners.")


def test_refusal_detection_covers_claude_phrasing():
    # Claude Haiku 시험 실행(2026-11-25)에서 거절인데 못 잡은 답변
    assert is_refusal("제공된 자료에는 2030년 주당 배당금 계획이 없어서 답변드릴 수 없습니다.")


def test_extract_numbers_drops_single_digits():
    assert extract_numbers("[1] 8,709억 12.5% 5") == {"8709", "12.5"}


def test_unsupported_numbers_ignores_single_digits_and_question_numbers():
    answer = "[1] 2024년 매출은 300조 8,709억원, 영업이익률 12.5%"
    ctx = ["매출액 300조 8,709억원"]
    assert unsupported_numbers(answer, ctx, "2024년 매출은?") == 1


def test_unsupported_numbers_accepts_year_inside_dotted_date():
    ctx = ["기준일 2024.12.31 매출 100억"]
    assert unsupported_numbers("2024년 매출 100억", ctx, "매출은?") == 0


def test_retrieved_coerces_section_order_to_int():
    r = Retrieved("R1", "R1.xml", "3", "1. 사업의 개요", None, "")
    assert r.section_order == 3
    assert hit_strict([r], [_exp()]) == 1
    assert mrr([r], [_exp()]) == 1.0


def _item(answerable=True):
    return Item(
        id="q1", corp_code="00126380", question="매출은?", answerable=answerable,
        origin="manual", must_include=["300조"], expected_sections=[_exp()] if answerable else [],
    )


def test_score_item_answerable():
    m = score_item(_item(), [_ret(text="300조")], "매출은 300조입니다.")
    assert m == {"hit_strict": 1, "hit_loose": 1, "mrr": 1.0, "fact_recall": 1.0,
                 "false_refusal": 0, "unsupported_numbers": 0}


def test_score_item_answerable_false_refusal():
    m = score_item(_item(), [], "확인할 수 없습니다.")
    assert m["false_refusal"] == 1 and "correct_refusal" not in m


def test_score_item_unanswerable():
    m = score_item(_item(False), [], "확인할 수 없습니다.")
    assert m == {"correct_refusal": 1, "unsupported_numbers": 0}
    assert score_item(_item(False), [], "매출은 300조입니다.")["correct_refusal"] == 0


def test_aggregate_skips_errors_and_reports_denominators():
    rows = [{"metrics": {"hit_strict": 1, "unsupported_numbers": 0}},
            {"metrics": {"hit_strict": 0, "unsupported_numbers": 2}},
            {"metrics": {"correct_refusal": 1, "unsupported_numbers": 0}},
            {"metrics": None, "error": "timeout"}]
    s = aggregate(rows)
    assert s["hit_strict"] == {"mean": 0.5, "n": 2}
    assert s["correct_refusal"] == {"mean": 1.0, "n": 1}
    assert s["unsupported_numbers"] == {"mean": pytest.approx(2 / 3), "n": 3}


def test_aggregate_includes_prompt_tokens_and_judge():
    rows = [{"metrics": {"hit_strict": 1}, "prompt_tokens": 2000,
             "judge": {"faithfulness": 1.0, "correct": True, "reason": ""}},
            {"metrics": {"hit_strict": 0}, "prompt_tokens": None,
             "judge": {"faithfulness": 0.5, "correct": False, "reason": ""}}]
    s = aggregate(rows)
    assert s["prompt_tokens"] == {"mean": 2000, "n": 1}
    assert s["faithfulness"] == {"mean": 0.75, "n": 2} and s["judge_correct"] == {"mean": 0.5, "n": 2}


def test_aggregate_by_type_groups_and_labels_unanswerable():
    rows = [{"pblntf_ty": "A", "answerable": True, "metrics": {"hit_strict": 1}},
            {"pblntf_ty": "I", "answerable": True, "metrics": {"hit_strict": 0}},
            {"pblntf_ty": None, "answerable": False, "metrics": {"correct_refusal": 1}}]
    s = aggregate_by_type(rows)
    assert set(s) == {"A", "I", "없음"}
    assert s["A"]["hit_strict"] == {"mean": 1.0, "n": 1}
    assert s["없음"]["correct_refusal"] == {"mean": 1.0, "n": 1}


def test_retrieved_allows_news_chunk_without_section_fields():
    r = Retrieved("1", None, None, None, None, "뉴스 본문")
    assert r.section_order is None and r.key == ("1", None, None)
    assert hit_strict([r], [_exp()]) == 0 and hit_loose([r], [_exp()]) == 0
    assert mrr([r], [_exp()]) == 0.0


def test_unsupported_numbers_ignores_numbers_in_cited_header():
    r = Retrieved("R1", "R1.xml", 3, "4. 매출 및 수주상황", None, "매출액은 300조원", "삼성전자", "사업보고서 (2024.12)")
    answer = "매출액은 300조원입니다. (근거: 사업보고서 (2024.12) > 4. 매출 및 수주상황)"
    assert score_item(_item(), [r], answer)["unsupported_numbers"] == 0


def _rec(name, ok=True, sources=(), result_text=""):
    from app.agent.trace import ToolCallRecord

    return ToolCallRecord(seq=1, name=name, arguments={}, ok=ok, result_text=result_text, sources=list(sources))


def _tool_item(**kw):
    return Item(id="q1", corp_code="00126380", question="매출액은?", answerable=False, origin="manual", **kw)


def test_score_tools_selection_and_unnecessary_web():
    from app.eval.metrics import score_tools

    item = _tool_item(expected_tools=["get_financials"])
    recs = [_rec("search_disclosures", ok=True), _rec("web_search", ok=False)]
    assert score_tools(item, recs) == {"tool_calls": 2, "tool_errors": 1, "tool_selection": 0, "unnecessary_web": 1}
    assert set(score_tools(_tool_item(), recs)) == {"tool_calls", "tool_errors"}
    ok = score_tools(_tool_item(expected_tools=["web_search"]), recs)
    assert ok["tool_selection"] == 1 and ok["unnecessary_web"] == 0


def _chunk(order):
    return {"kind": "chunk", "corp_name": "삼성", "report_nm": "사업보고서", "rcept_no": "R1",
            "source_file": "R1.xml", "section_order": order, "section_title": "개요", "doc_type": None,
            "score": 0.5, "text": f"본문{order}"}


def test_retrieved_from_records_keeps_call_order():
    from app.eval.metrics import retrieved_from_records

    recs = [
        _rec("search_disclosures", sources=[_chunk(1)]),
        _rec("web_search", sources=[{"kind": "web", "url": "u"}]),
        _rec("search_disclosures", sources=[_chunk(2), {"kind": "other"}]),
    ]
    got = retrieved_from_records(recs)
    assert [r.section_order for r in got] == [1, 2]
    assert got[0].text == "본문1" and got[0].corp_name == "삼성" and got[0].score == 0.5
    assert retrieved_from_records([_rec("web_search")]) == []


def test_score_item_uses_context_texts_for_numbers():
    item = _tool_item()
    answer = "영업이익은 6,567,000백만원입니다."
    assert score_item(item, [], answer)["unsupported_numbers"] == 1
    assert score_item(item, [], answer, context_texts=["영업이익 6567000백만원"])["unsupported_numbers"] == 0
