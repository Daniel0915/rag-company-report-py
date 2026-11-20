"""규칙 기반 채점(검색 적중, 사실 포함, 거절, 근거 없는 숫자)과 집계. 순수 함수만 둔다."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from app.eval.dataset import Item, Section

REFUSAL_PHRASES: tuple[str, ...] = (
    "모릅니다",
    "모르겠",
    "알 수 없",
    "확인할 수 없",
    "찾을 수 없",
    "포함되어 있지 않",
    "정보가 없",
    "나와 있지 않",
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_NO_TYPE = "없음"


@dataclass(frozen=True)
class Retrieved:
    rcept_no: str
    source_file: str
    section_order: int
    section_title: str
    score: float | None
    text: str

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.rcept_no, self.source_file, self.section_order)


def hit_strict(retrieved: list[Retrieved], expected: list[Section]) -> int:
    keys = {s.key for s in expected}
    return int(any(r.key in keys for r in retrieved))


def hit_loose(retrieved: list[Retrieved], expected: list[Section]) -> int:
    """엄격 일치이거나 섹션 제목이 기대 섹션과 같으면 1."""
    titles = {s.section_title for s in expected}
    return int(hit_strict(retrieved, expected) or any(r.section_title in titles for r in retrieved))


def mrr(retrieved: list[Retrieved], expected: list[Section]) -> float:
    keys = {s.key for s in expected}
    for rank, r in enumerate(retrieved, start=1):
        if r.key in keys:
            return 1 / rank
    return 0.0


def _squash(text: str) -> str:
    return re.sub(r"[\s,]", "", text)


def fact_recall(answer: str, must_include: list[str]) -> float:
    if not must_include:
        return 1.0
    squashed = _squash(answer)
    return sum(_squash(f) in squashed for f in must_include) / len(must_include)


def is_refusal(answer: str) -> bool:
    return any(p in answer for p in REFUSAL_PHRASES)


def extract_numbers(text: str) -> set[str]:
    """숫자 토큰(쉼표 제거)을 뽑는다. 한 자리 정수(목록 번호 등)는 버린다."""
    numbers = {m.replace(",", "") for m in _NUMBER.findall(text)}
    return {n for n in numbers if len(n) > 1 or not n.isdigit()}


def unsupported_numbers(answer: str, context_texts: list[str], question: str) -> int:
    """답변 숫자 중 근거에도 질문에도 없는 것의 개수."""
    supported = extract_numbers(question)
    for text in context_texts:
        supported |= extract_numbers(text)
    return len(extract_numbers(answer) - supported)


def score_item(item: Item, retrieved: list[Retrieved], answer: str) -> dict[str, float | int]:
    unsupported = unsupported_numbers(answer, [r.text for r in retrieved], item.question)
    if not item.answerable:
        return {"correct_refusal": int(is_refusal(answer)), "unsupported_numbers": unsupported}
    return {
        "hit_strict": hit_strict(retrieved, item.expected_sections),
        "hit_loose": hit_loose(retrieved, item.expected_sections),
        "mrr": mrr(retrieved, item.expected_sections),
        "fact_recall": fact_recall(answer, item.must_include),
        "false_refusal": int(is_refusal(answer)),
        "unsupported_numbers": unsupported,
    }


def aggregate(rows: list[dict]) -> dict[str, dict[str, float | int]]:
    """오류 행(metrics=None)을 빼고, 지표마다 그 지표를 가진 행만으로 평균과 분모를 낸다."""
    values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get("metrics") is None:
            continue
        for name, value in row["metrics"].items():
            values[name].append(value)
        if row.get("prompt_tokens") is not None:
            values["prompt_tokens"].append(row["prompt_tokens"])
        if row.get("judge"):
            values["faithfulness"].append(row["judge"]["faithfulness"])
            values["judge_correct"].append(int(row["judge"]["correct"]))
    return {name: {"mean": sum(v) / len(v), "n": len(v)} for name, v in values.items()}


def aggregate_by_type(rows: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row.get("pblntf_ty") or _NO_TYPE].append(row)
    return {ty: aggregate(group) for ty, group in groups.items()}
