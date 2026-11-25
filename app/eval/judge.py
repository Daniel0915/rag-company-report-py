"""Claude CLI로 답변을 채점한다(선택). 근거 충실도와 정답 여부를 JSON으로 받는다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.eval.dataset import Item
from app.eval.generate import extract_json_object
from app.eval.metrics import Retrieved
from app.llm.claude_cli import ClaudeCliClient

_SYSTEM_PROMPT = "너는 공시 기반 질의응답 답변을 채점하는 평가자다. 지시한 JSON 객체 하나만 출력한다."
_UNANSWERABLE_NOTE = "공시에 답이 없는 질문이다."
_CORRECT_ANSWERABLE = "- correct: 기대 답과 의미가 일치하는가. 핵심 사실과 숫자가 같으면 true(true/false)\n"
_CORRECT_UNANSWERABLE = "- correct: 공시에 답이 없는 질문이므로, 모른다고 적절히 거절했을 때만 true, 답을 지어냈으면 false(true/false)\n"


@dataclass(frozen=True)
class JudgeResult:
    faithfulness: float
    correct: bool
    reason: str


def build_judge_prompt(
    item: Item, retrieved: list[Retrieved], answer: str, evidence_texts: list[str] | None = None
) -> str:
    """evidence_texts가 있으면(에이전트 경로: 모든 도구 결과) 검색 청크 대신 그 텍스트를 근거로 쓴다."""
    texts = evidence_texts if evidence_texts is not None else [r.text for r in retrieved]
    evidence = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1)) or "(검색된 근거 없음)"
    expected = f"기대 답: {item.expected_answer}" if item.answerable else _UNANSWERABLE_NOTE
    return (
        f"질문: {item.question}\n\n"
        f"근거 텍스트:\n{evidence}\n\n"
        f"답변:\n{answer}\n\n"
        f"{expected}\n\n"
        "다음을 채점해 JSON 객체 하나만 출력해라.\n"
        "- faithfulness: 답변의 사실 주장 중 번호 붙은 근거 텍스트로 뒷받침되는 비율(0~1). "
        "근거에 없는 주장이 있으면 낮추고, 사실 주장이 없는 답변(예: 거절)은 1.0\n"
        f"{_CORRECT_ANSWERABLE if item.answerable else _CORRECT_UNANSWERABLE}"
        "- reason: 판단 이유 한 문장\n"
        '{"faithfulness": 0.0, "correct": true, "reason": "..."}'
    )


def parse_judge(raw: str) -> JudgeResult | None:
    data = extract_json_object(raw)
    if data is None:
        return None
    faithfulness, correct, reason = data.get("faithfulness"), data.get("correct"), data.get("reason")
    if isinstance(faithfulness, bool) or not isinstance(faithfulness, (int, float)):
        return None
    if not 0 <= faithfulness <= 1 or not isinstance(correct, bool) or not isinstance(reason, str):
        return None
    return JudgeResult(float(faithfulness), correct, reason)


def judge(
    claude: ClaudeCliClient,
    item: Item,
    retrieved: list[Retrieved],
    answer: str,
    on_error: Callable[[str], None] | None = None,
    evidence_texts: list[str] | None = None,
) -> JudgeResult | None:
    """호출 예외나 파싱 실패면 None. 실패 사유는 on_error로 알린다."""
    try:
        raw = claude.generate(_SYSTEM_PROMPT, build_judge_prompt(item, retrieved, answer, evidence_texts))
    except Exception as exc:  # noqa: BLE001 - 채점은 문항별 선택 기능이라 어떤 호출 오류도 건너뛴다
        if on_error:
            on_error(f"{type(exc).__name__}: {exc}")
        return None
    result = parse_judge(raw)
    if result is None and on_error:
        on_error(f"채점 응답 파싱 실패: {raw[:200]!r}")
    return result
