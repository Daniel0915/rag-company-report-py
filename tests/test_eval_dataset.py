import pytest
import yaml

from app.eval.cli import main
from app.eval.dataset import (
    Candidate,
    DatasetError,
    Item,
    Section,
    accept,
    existing_section_keys,
    file_hash,
    load_candidates,
    load_items,
    save_candidates,
    save_items,
)


def _section(order: int = 3) -> Section:
    return Section(rcept_no="20240312000736", source_file="main.xml", section_order=order, section_title="사업의 내용")


def _item(item_id: str = "q0001", **overrides) -> Item:
    fields = dict(
        id=item_id,
        corp_code="00126380",
        question="주요 사업은?",
        answerable=True,
        origin="generated",
        expected_answer="반도체",
        must_include=["반도체"],
        expected_sections=[_section()],
    )
    fields.update(overrides)
    return Item(**fields)


def _candidate(item_id: str, review: str) -> Candidate:
    return Candidate(**{**_item(item_id).__dict__, "review": review, "source_chunk_text": "원문"})


def test_roundtrip_keeps_korean_and_corp_code_string(tmp_path):
    path = tmp_path / "dataset.yaml"
    item = _item(corp_code="00104252", question="삼성전자의 주요 사업은?")
    save_items(path, [item])

    assert "삼성전자" in path.read_text(encoding="utf-8")
    loaded = load_items(path)
    assert loaded == [item]
    assert loaded[0].corp_code == "00104252"


def test_unquoted_corp_code_is_rejected(tmp_path):
    path = tmp_path / "dataset.yaml"
    path.write_text(
        "- id: m0001\n  corp_code: 12345\n  question: q\n  answerable: false\n  origin: manual\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="따옴표"):
        load_items(path)

    path.write_text(
        "- id: m0001\n  corp_code: 00126380\n  question: q\n  answerable: false\n  origin: manual\n",
        encoding="utf-8",
    )
    assert load_items(path)[0].corp_code == "00126380"


def test_answerable_without_must_include_is_rejected(tmp_path):
    path = tmp_path / "dataset.yaml"
    save_items(path, [_item(must_include=[])])
    with pytest.raises(DatasetError, match="q0001"):
        load_items(path)


def test_unanswerable_needs_no_sections(tmp_path):
    path = tmp_path / "dataset.yaml"
    save_items(path, [_item(answerable=False, must_include=[], expected_sections=[], expected_answer=None)])
    assert load_items(path)[0].answerable is False


def test_duplicate_id_origin_and_review_are_rejected(tmp_path):
    path = tmp_path / "dataset.yaml"
    save_items(path, [_item("q0001"), _item("q0001")])
    with pytest.raises(DatasetError, match="q0001"):
        load_items(path)

    save_items(path, [_item(origin="auto")])
    with pytest.raises(DatasetError, match="origin"):
        load_items(path)

    save_candidates(path, [_candidate("q0001", "maybe")])
    with pytest.raises(DatasetError, match="review"):
        load_candidates(path)


def test_missing_file_loads_as_empty(tmp_path):
    assert load_items(tmp_path / "none.yaml") == []
    assert load_candidates(tmp_path / "none.yaml") == []


def test_accept_moves_ok_drops_drop_keeps_pending(tmp_path):
    cands, data = tmp_path / "candidates.yaml", tmp_path / "dataset.yaml"
    save_candidates(cands, [_candidate("c1", "ok"), _candidate("c2", "drop"), _candidate("c3", "pending")])

    assert accept(cands, data) == (1, 1)

    assert [c.id for c in load_candidates(cands)] == ["c3"]
    assert len(load_items(data)) == 1
    moved = yaml.safe_load(data.read_text(encoding="utf-8"))[0]
    assert "review" not in moved and "source_chunk_text" not in moved


def test_accept_assigns_ids_after_existing_max(tmp_path):
    cands, data = tmp_path / "candidates.yaml", tmp_path / "dataset.yaml"
    save_items(data, [_item("q0007"), _item("m0001", origin="manual")])
    save_candidates(cands, [_candidate("c1", "ok"), _candidate("c2", "ok")])

    accept(cands, data)

    assert [i.id for i in load_items(data)] == ["q0007", "m0001", "q0008", "q0009"]


def test_cli_accept_prints_counts(tmp_path, capsys):
    cands, data = tmp_path / "candidates.yaml", tmp_path / "dataset.yaml"
    save_candidates(cands, [_candidate("c1", "ok"), _candidate("c2", "drop")])

    assert main(["accept", "--candidates", str(cands), "--dataset", str(data)]) == 0

    out = capsys.readouterr().out
    assert "옮김 1" in out and "버림 1" in out


def test_file_hash_changes_with_content_and_section_keys_cover_both_files(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    save_items(a, [_item(expected_sections=[_section(1)])])
    save_candidates(b, [_candidate("c1", "pending")])
    before = file_hash(a)
    save_items(a, [_item(expected_sections=[_section(2)])])

    assert file_hash(a) != before
    assert existing_section_keys(a, b) == {("20240312000736", "main.xml", 2), ("20240312000736", "main.xml", 3)}


def test_malformed_yaml_becomes_dataset_error(tmp_path, capsys):
    path = tmp_path / "dataset.yaml"
    path.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(DatasetError):
        load_items(path)

    path.write_text(
        "- id: q0001\n  corp_code: '00126380'\n  question: q\n  answerable: true\n  origin: manual\n"
        "  must_include: [a]\n  expected_sections:\n    - rcept_no: '1'\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="q0001"):
        load_items(path)

    cands = tmp_path / "candidates.yaml"
    assert main(["accept", "--candidates", str(cands), "--dataset", str(path)]) == 1
    assert "오류" in capsys.readouterr().out
