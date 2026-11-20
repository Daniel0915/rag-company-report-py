"""Claude CLI로 답변을 채점한다(선택). 근거 충실도와 정답 여부를 JSON으로 받는다."""

from __future__ import annotations

from dataclasses import dataclass

from app.eval.dataset import Item
from app.eval.generate import extract_json_object
from app.eval.metrics import Retrieved
from app.llm.claude_cli import ClaudeCliClient

_SYSTEM_PROMPT = "너는 공시 기반 질의응답 답변을 채점하는 평가자다. 지시한 JSON 객체 하나만 출력한다."
_UNANSWERABLE_NOTE = "공시에 답이 없는 질문이다. 적절히 거절했으면 correct=true"


@dataclass(frozen=True)
class JudgeResult:
    faithfulness: float
    correct: bool
    reason: str


def build_judge_prompt(item: Item, retrieved: list[Retrieved], answer: str) -> str:
    evidence = "\n".join(f"{i}. {r.text}" for i, r in enumerate(retrieved, 1)) or "(검색된 근거 없음)"
    expected = f"기대 답: {item.expected_answer}" if item.answerable else _UNANSWERABLE_NOTE
    return (
        f"질문: {item.question}\n\n"
        f"근거 텍스트:\n{evidence}\n\n"
        f"답변:\n{answer}\n\n"
        f"{expected}\n\n"
        "다음을 채점해 JSON 객체 하나만 출력해라.\n"
        "- faithfulness: 답변의 주장이 근거 텍스트로 뒷받침되는 정도(0~1)\n"
        "- correct: 답변이 질문에 맞게 답했는가(true/false)\n"
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


def judge(claude: ClaudeCliClient, item: Item, retrieved: list[Retrieved], answer: str) -> JudgeResult | None:
    """호출 예외나 파싱 실패면 None."""
    try:
        raw = claude.generate(_SYSTEM_PROMPT, build_judge_prompt(item, retrieved, answer))
    except RuntimeError:
        return None
    return parse_judge(raw)
