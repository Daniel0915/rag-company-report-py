"""규칙 기반 채점(검색 적중, 사실 포함, 거절, 근거 없는 숫자)과 집계. 순수 함수만 둔다."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from app.agent.trace import ToolCallRecord
from app.eval.dataset import Item, Section
from app.services.company_chat import _build_context_text
from app.vector.document import Document

REFUSAL_PHRASES: tuple[str, ...] = (
    "모릅니다",
    "모르겠",
    "알 수 없",
    "확인할 수 없",
    "찾을 수 없",
    "포함되어 있지 않",
    "정보가 없",
    "나와 있지 않",
    "명시되어 있지 않",
    "제공되지 않",
    "답변드릴 수 없",
    "답변할 수 없",
    "답할 수 없",
    "찾지 못했",
    "확인하지 못했",
    "나오지 않았",
    "존재하지 않",
    # 로컬 모델이 영어로 거절하는 경우
    "not provided",
    "does not contain",
    "not available",
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_NO_TYPE = "없음"


@dataclass(frozen=True)
class Retrieved:
    """검색된 청크. 뉴스·어드민 PDF 청크는 source_file·section_order 등이 없어 None일 수 있다."""

    rcept_no: str | None
    source_file: str | None
    section_order: int | None
    section_title: str | None
    score: float | None
    text: str
    corp_name: str | None = None
    report_nm: str | None = None

    def __post_init__(self) -> None:
        # Neo4j 메타데이터가 문자열로 줄 수 있어 정수로 맞춘다.
        if self.section_order is not None:
            object.__setattr__(self, "section_order", int(self.section_order))

    @property
    def key(self) -> tuple[str | None, str | None, int | None]:
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


# 거절은 답의 첫머리에서 한다. 끝에 붙은 단서("…는 명시되어 있지 않습니다")까지 보면
# 답을 다 한 경우도 거절로 센다(Claude 기존 경로에서 24문항 중 17개가 그렇게 잡혔다).
REFUSAL_HEAD_CHARS = 200


def is_refusal(answer: str) -> bool:
    head = answer[:REFUSAL_HEAD_CHARS]
    return any(p in head for p in REFUSAL_PHRASES)


def extract_numbers(text: str) -> set[str]:
    """숫자 토큰(쉼표 제거)을 뽑는다. 한 자리 정수(목록 번호 등)는 버린다."""
    numbers = {m.replace(",", "") for m in _NUMBER.findall(text)}
    return {n for n in numbers if len(n) > 1 or not n.isdigit()}


def unsupported_numbers(answer: str, context_texts: list[str], question: str) -> int:
    """답변 숫자 중 근거에도 질문에도 없는 것의 개수."""
    supported: set[str] = set()
    for text in [question, *context_texts]:
        # 2024.12.31 같은 날짜 안의 연도도 근거로 인정하려고 숫자 덩어리도 함께 담는다.
        supported |= extract_numbers(text) | set(re.findall(r"\d+", text.replace(",", "")))
    return len(extract_numbers(answer) - supported)


def _seen_by_model(retrieved: list[Retrieved]) -> list[str]:
    """모델이 본 컨텍스트(출처 머리말 + 본문). 채팅과 같은 형식을 쓰려고 company_chat의 것을 재사용한다."""
    docs = [
        Document(
            text=r.text,
            metadata={"corp_name": r.corp_name, "report_nm": r.report_nm, "section_title": r.section_title},
            score=r.score,
        )
        for r in retrieved
    ]
    return [_build_context_text([doc]) for doc in docs]


def score_item(
    item: Item, retrieved: list[Retrieved], answer: str, context_texts: list[str] | None = None
) -> dict[str, float | int]:
    """context_texts가 있으면(에이전트 경로: 모든 도구 결과 텍스트) 숫자 근거로 청크 대신 그것을 쓴다."""
    seen = _seen_by_model(retrieved) if context_texts is None else context_texts
    unsupported = unsupported_numbers(answer, seen, item.question)
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


def score_tools(item: Item, records: list[ToolCallRecord]) -> dict[str, int]:
    """도구 호출 지표. tool_selection·unnecessary_web은 expected_tools를 채운 문항에서만 낸다."""
    metrics = {"tool_calls": len(records), "tool_errors": sum(not r.ok for r in records)}
    if item.expected_tools:
        used = {r.name for r in records}
        metrics["tool_selection"] = int(bool(used & set(item.expected_tools)))
        metrics["unnecessary_web"] = int("web_search" in used and "web_search" not in item.expected_tools)
    return metrics


def retrieved_from_records(records: list[ToolCallRecord]) -> list[Retrieved]:
    """search_disclosures가 돌려준 청크 출처를 호출 순서대로 이어 붙인다."""
    return [
        Retrieved(
            rcept_no=s.get("rcept_no"),
            source_file=s.get("source_file"),
            section_order=s.get("section_order"),
            section_title=s.get("section_title"),
            score=s.get("score"),
            text=s.get("text", ""),
            corp_name=s.get("corp_name"),
            report_nm=s.get("report_nm"),
        )
        for rec in records
        if rec.name == "search_disclosures"
        for s in rec.sources
        if s.get("kind") == "chunk"
    ]


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
