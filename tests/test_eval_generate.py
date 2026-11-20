import random

import pytest

from app.eval import generate as gen
from app.eval.cli import main
from app.eval.dataset import Section, load_candidates
from app.eval.generate import (
    ChunkRow,
    build_generation_prompt,
    parse_generation,
    parse_quota,
    pick_by_quota,
)


def _rows(ty: str, sections: int, per_section: int = 1) -> list[ChunkRow]:
    return [
        ChunkRow(
            chunk_id=f"{ty}-{s:03d}-{c}",
            pblntf_ty=ty,
            report_nm="보고서",
            section=Section(rcept_no=f"{ty}{s:03d}", source_file="main.xml", section_order=s, section_title=f"섹션{s}"),
        )
        for s in range(sections)
        for c in range(per_section)
    ]


def test_parse_quota():
    assert parse_quota("A=6,I=4") == {"A": 6, "I": 4}
    with pytest.raises(ValueError):
        parse_quota("Z=3")
    with pytest.raises(ValueError):
        parse_quota("A=0")
    with pytest.raises(ValueError):
        parse_quota("A")


def test_pick_by_quota_respects_counts_and_one_chunk_per_section():
    rows = _rows("A", 20, 2) + _rows("I", 10)
    picked, counts = pick_by_quota(rows, {"A": 3, "I": 2}, set(), random.Random(1))
    assert len(picked) == 5
    assert counts == {"A": 3, "I": 2}
    assert len({r.section.key for r in picked}) == 5


def test_pick_by_quota_skips_excluded_sections():
    rows = _rows("A", 4)
    exclude = {rows[0].section.key, rows[1].section.key}
    picked, counts = pick_by_quota(rows, {"A": 4}, exclude, random.Random(1))
    assert {r.chunk_id for r in picked} == {rows[2].chunk_id, rows[3].chunk_id}
    assert counts == {"A": 2}


def test_pick_by_quota_fills_shortfall_from_A():
    rows = _rows("A", 20) + _rows("B", 1)
    picked, counts = pick_by_quota(rows, {"A": 2, "B": 3}, set(), random.Random(1))
    assert counts == {"A": 4, "B": 1}
    assert len(picked) == 5


def test_pick_by_quota_is_reproducible_with_seed():
    rows = _rows("A", 20, 2) + _rows("I", 10)
    shuffled = rows[:]
    random.Random(3).shuffle(shuffled)
    first, _ = pick_by_quota(rows, {"A": 3, "I": 2}, set(), random.Random(7))
    second, _ = pick_by_quota(shuffled, {"A": 3, "I": 2}, set(), random.Random(7))
    assert [r.chunk_id for r in first] == [r.chunk_id for r in second]


def test_parse_generation_accepts_fenced_json():
    raw = '여기 있습니다\n```json\n{"question": "q", "answer": "a", "must_include": ["1조"]}\n```'
    assert parse_generation(raw)["must_include"] == ["1조"]


def test_parse_generation_rejects_missing_facts_and_garbage():
    assert parse_generation('{"question": "q", "answer": "a", "must_include": []}') is None
    assert parse_generation('{"question": "q", "answer": "a"}') is None
    assert parse_generation("JSON 아님") is None


def test_generation_prompt_forbids_relative_time():
    p = build_generation_prompt("본문", "2. 영업실적", "영업(잠정)실적(공정공시)")
    assert "최근" in p and "본문" in p


class _FakeClaude:
    def __init__(self, replies):
        self._replies = list(replies)

    def generate(self, system_prompt, user_prompt):
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_generate_appends_candidates_and_counts_failures(tmp_path, monkeypatch):
    rows = _rows("A", 3)
    monkeypatch.setattr(gen, "fetch_chunk_rows", lambda driver, corp: rows)
    monkeypatch.setattr(gen, "fetch_texts", lambda driver, corp, ids: {i: f"본문 {i}" for i in ids})
    good = '{"question": "2024년 매출은?", "answer": "1조", "must_include": ["1조"]}'
    claude = _FakeClaude([good, "깨진 응답", RuntimeError("timeout")])
    cands = tmp_path / "c.yaml"
    result = gen.generate(None, claude, "00126380", {"A": 3}, 1, cands, tmp_path / "d.yaml")
    assert result == {"picked": {"A": 3}, "added": 1, "parse_failed": 1, "call_failed": 1}
    saved = load_candidates(cands)
    assert [c.id for c in saved] == ["c0001"]
    assert saved[0].pblntf_ty == "A" and saved[0].review == "pending"
    assert saved[0].source_chunk_text.startswith("본문")
    assert saved[0].expected_sections[0].source_file == "main.xml"

    # 다시 실행하면 이미 쓴 섹션은 피하고 번호는 이어진다
    monkeypatch.setattr(gen, "fetch_chunk_rows", lambda driver, corp: rows + _rows("A", 5)[3:])
    claude = _FakeClaude([good, good])
    result = gen.generate(None, claude, "00126380", {"A": 2}, 1, cands, tmp_path / "d.yaml")
    assert result["added"] == 2
    assert [c.id for c in load_candidates(cands)] == ["c0001", "c0002", "c0003"]


def test_cli_generate_rejects_unknown_corp_before_connecting(capsys):
    assert main(["generate", "--corp", "99999999"]) == 1
    assert "99999999" in capsys.readouterr().out
