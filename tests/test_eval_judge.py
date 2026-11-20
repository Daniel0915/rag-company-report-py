from app.eval.dataset import Item
from app.eval.judge import JudgeResult, build_judge_prompt, judge, parse_judge
from app.eval.metrics import Retrieved


def _item(answerable: bool = True) -> Item:
    return Item(
        id="q1",
        corp_code="00126380",
        question="매출액은 얼마야?",
        answerable=answerable,
        origin="manual",
        expected_answer="300조 8,709억원이다." if answerable else "",
        must_include=["300조"] if answerable else [],
        expected_sections=[],
        pblntf_ty="A",
    )


def _retrieved() -> list[Retrieved]:
    return [Retrieved("1", "a.xml", 0, "개요", 0.9, "매출액은 300조 8,709억원")]


class _Fake:
    def __init__(self, reply: str | Exception):
        self.reply = reply

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_parse_judge_accepts_text_around_json():
    r = parse_judge('판단: {"faithfulness": 0.8, "correct": true, "reason": "근거 일치"} 끝')
    assert r == JudgeResult(0.8, True, "근거 일치")


def test_parse_judge_rejects_out_of_range():
    assert parse_judge('{"faithfulness": 1.5, "correct": true, "reason": "x"}') is None


def test_parse_judge_rejects_missing_key():
    assert parse_judge('{"faithfulness": 0.5, "correct": true}') is None


def test_judge_prompt_includes_evidence_answer_and_expected():
    p = build_judge_prompt(_item(), _retrieved(), "300조입니다")
    assert "매출액은 얼마야?" in p and "1. " in p and "300조 8,709억원" in p
    assert "300조입니다" in p and "거절했으면" not in p


def test_judge_prompt_for_unanswerable_mentions_refusal():
    p = build_judge_prompt(_item(False), _retrieved(), "모릅니다")
    assert "거절" in p and "기대 답" not in p


def test_judge_returns_none_on_cli_error():
    assert judge(_Fake(RuntimeError("boom")), _item(), _retrieved(), "a") is None


def test_judge_returns_result():
    reply = '{"faithfulness": 1, "correct": false, "reason": "r"}'
    assert judge(_Fake(reply), _item(), _retrieved(), "a") == JudgeResult(1.0, False, "r")
