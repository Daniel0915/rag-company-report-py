"""공시 유형별 할당 샘플링과 Claude CLI 문항 생성(`generate`)."""

from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.eval.dataset import Candidate, Section, existing_section_keys, load_candidates, save_candidates
from app.llm.claude_cli import ClaudeCliClient
from app.vector.registry import label_for

DEFAULT_QUOTA: dict[str, int] = {"A": 12, "I": 8, "B": 5, "D": 3, "J": 1, "E": 1}
MIN_CHUNK_CHARS = 200
EVAL_CLAUDE_TIMEOUT_SECONDS = 180
FILL_TYPE = "A"

_TYPE_RE = re.compile(r"^[A-J]$")
_CANDIDATE_ID = re.compile(r"^c(\d+)$")
_FENCE = re.compile(r"```(?:json)?")

_SYSTEM_PROMPT = "너는 공시 기반 질의응답 평가용 문항을 만드는 도우미다. 지시한 JSON 객체 하나만 출력한다."

_CHUNK_ROWS_QUERY = """
MATCH (n:`{label}`)
WHERE n.`metadata.doc_type` IS NULL AND size(n.text) >= $min_chars
OPTIONAL MATCH (r:Report {{rcept_no: n.`metadata.rcept_no`}})
WITH n, coalesce(n.`metadata.pblntf_ty`, r.pblntf_ty) AS ty
WHERE ty IS NOT NULL
RETURN n.id AS id, ty, n.`metadata.rcept_no` AS rcept_no, n.`metadata.source_file` AS source_file,
       n.`metadata.section_order` AS section_order, n.`metadata.section_title` AS section_title,
       n.`metadata.report_nm` AS report_nm
"""

_TEXTS_QUERY = "MATCH (n:`{label}`) WHERE n.id IN $ids RETURN n.id AS id, n.text AS text"


@dataclass(frozen=True)
class ChunkRow:
    chunk_id: str
    pblntf_ty: str
    report_nm: str
    section: Section


def parse_quota(text: str) -> dict[str, int]:
    """`"A=6,I=4"` 형식을 `{"A": 6, "I": 4}`로 읽는다. 지정한 유형만 담는다."""
    quota: dict[str, int] = {}
    for part in text.split(","):
        ty, sep, count = part.strip().partition("=")
        if not sep or not _TYPE_RE.match(ty) or not count.isdigit() or int(count) < 1:
            raise ValueError(f"할당은 'A=6,I=4'처럼 유형(A~J)=양의 정수로 쓰세요 ({part.strip()!r})")
        quota[ty] = int(count)
    return quota


def pick_by_quota(
    rows: list[ChunkRow], quota: dict[str, int], exclude: set[tuple], rng: random.Random
) -> tuple[list[ChunkRow], dict[str, int]]:
    """유형마다 할당만큼 섹션을 무작위로 뽑고(섹션당 청크 하나), 모자란 개수는 A에서 채운다."""
    by_section: dict[str, dict[tuple, list[ChunkRow]]] = defaultdict(lambda: defaultdict(list))
    for row in sorted(rows, key=lambda r: r.chunk_id):
        if row.section.key not in exclude:
            by_section[row.pblntf_ty][row.section.key].append(row)

    picked: list[ChunkRow] = []
    counts: dict[str, int] = {}

    def take(ty: str, want: int) -> int:
        sections = by_section[ty]
        keys = sorted(sections)
        chosen = rng.sample(keys, min(want, len(keys)))
        for key in chosen:
            picked.append(rng.choice(sections.pop(key)))
        return len(chosen)

    shortfall = 0
    for ty in sorted(quota):
        got = take(ty, quota[ty])
        counts[ty] = got
        shortfall += quota[ty] - got
    if shortfall:
        counts[FILL_TYPE] = counts.get(FILL_TYPE, 0) + take(FILL_TYPE, shortfall)
    return picked, counts


def fetch_chunk_rows(driver, corp_code: str) -> list[ChunkRow]:
    query = _CHUNK_ROWS_QUERY.format(label=label_for(corp_code))
    records, _, _ = driver.execute_query(query, min_chars=MIN_CHUNK_CHARS)
    return [
        ChunkRow(
            chunk_id=r["id"],
            pblntf_ty=r["ty"],
            report_nm=r["report_nm"] or "",
            section=Section(
                rcept_no=r["rcept_no"],
                source_file=r["source_file"],
                section_order=int(r["section_order"]),
                section_title=r["section_title"] or "",
            ),
        )
        for r in records
    ]


def fetch_texts(driver, corp_code: str, chunk_ids: list[str]) -> dict[str, str]:
    query = _TEXTS_QUERY.format(label=label_for(corp_code))
    records, _, _ = driver.execute_query(query, ids=chunk_ids)
    return {r["id"]: r["text"] for r in records}


def build_generation_prompt(text: str, section_title: str, report_nm: str) -> str:
    return (
        f"아래는 공시 '{report_nm}'의 '{section_title}' 섹션에서 가져온 발췌다.\n\n"
        f"[발췌]\n{text}\n[/발췌]\n\n"
        "이 발췌만으로 답할 수 있는 질문 하나와 답, 핵심 사실을 JSON으로 써라.\n"
        "- 질문은 원문 문장을 베끼지 말고 투자자가 실제로 묻는 말투로 쓴다.\n"
        "- 질문에는 기간(연도·분기·날짜)을 분명히 넣는다. '최근', '이번' 같은 상대적인 시간 표현은 쓰지 않는다.\n"
        "- 핵심 사실(must_include)은 답에 반드시 들어가야 하는 숫자·이름이다. 숫자는 단위까지 발췌에 나온 그대로 쓴다.\n"
        '- 출력은 JSON 객체 하나만: {"question": "...", "answer": "...", "must_include": ["...", "..."]}'
    )


def extract_json_object(raw: str) -> dict | None:
    """코드 펜스를 벗기고 첫 `{`부터 마지막 `}`까지 JSON 객체로 읽는다. 실패하면 None."""
    text = _FENCE.sub("", raw)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_generation(raw: str) -> dict | None:
    data = extract_json_object(raw)
    if data is None:
        return None
    facts = data.get("must_include")
    if not isinstance(data.get("question"), str) or not isinstance(data.get("answer"), str):
        return None
    if not isinstance(facts, list) or not facts:
        return None
    return data


def _next_candidate_number(candidates: list[Candidate]) -> int:
    numbers = [int(m.group(1)) for c in candidates if (m := _CANDIDATE_ID.match(c.id))]
    return max(numbers, default=0) + 1


def generate(
    driver,
    claude: ClaudeCliClient,
    corp_code: str,
    quota: dict[str, int],
    seed: int | None,
    candidates_path: str | Path,
    dataset_path: str | Path,
) -> dict:
    """유형별로 청크를 뽑아 Claude로 문항을 만들고 후보 파일 끝에 붙인다."""
    rng = random.Random(seed)
    exclude = existing_section_keys(dataset_path, candidates_path)
    picked, counts = pick_by_quota(fetch_chunk_rows(driver, corp_code), quota, exclude, rng)
    texts = fetch_texts(driver, corp_code, [r.chunk_id for r in picked])

    candidates = load_candidates(candidates_path)
    number = _next_candidate_number(candidates)
    added = parse_failed = call_failed = 0
    first_error: str | None = None
    for row in picked:
        text = texts[row.chunk_id]
        try:
            raw = claude.generate(_SYSTEM_PROMPT, build_generation_prompt(text, row.section.section_title, row.report_nm))
        except Exception as exc:
            call_failed += 1
            if first_error is None:
                first_error = f"{type(exc).__name__}: {exc}"
            continue
        data = parse_generation(raw)
        if data is None:
            parse_failed += 1
            continue
        candidates.append(
            Candidate(
                id=f"c{number:04d}",
                corp_code=corp_code,
                question=data["question"],
                answerable=True,
                origin="generated",
                expected_answer=data["answer"],
                must_include=[str(f) for f in data["must_include"]],
                expected_sections=[row.section],
                pblntf_ty=row.pblntf_ty,
                review="pending",
                source_chunk_text=text,
            )
        )
        number += 1
        added += 1
        save_candidates(candidates_path, candidates)  # 긴 실행이 중간에 끊겨도 만든 것은 남긴다
    return {"picked": counts, "added": added, "parse_failed": parse_failed, "call_failed": call_failed, "first_error": first_error}
