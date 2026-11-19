import json

import pytest

from app.ingest.ledger import IndexLedger, LedgerError


def test_has_rcept_matches_any_file_of_the_disclosure(tmp_path):
    ledger = IndexLedger(tmp_path / "l.json")
    ledger.put("20250311001085/20250311001085_00760.xml", "h", ["c1"])
    assert ledger.has_rcept("20250311001085")
    assert not ledger.has_rcept("2025031100108")  # 접두어만 같은 번호는 아님


def test_save_and_reload_keep_existing_format(tmp_path):
    p = tmp_path / "l.json"
    ledger = IndexLedger(p)
    ledger.put("1/a.xml", "h", ["c1", "c2"])
    ledger.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "1/a.xml": {"hash": "h", "chunkIds": ["c1", "c2"]}
    }
    again = IndexLedger(p)
    assert again.get("1/a.xml").chunkIds == ["c1", "c2"]


def test_corrupt_ledger_raises(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(LedgerError):
        IndexLedger(p)


def test_mark_processed_without_files(tmp_path):
    ledger = IndexLedger(tmp_path / "l.json")
    ledger.mark_processed_without_files("9")
    assert ledger.has_rcept("9") and ledger.get("9/-").chunkIds == []


def test_import_from_refuses_when_not_empty(tmp_path):
    src = tmp_path / "src.json"
    src.write_text('{"1/a.xml": {"hash": "h", "chunkIds": []}}', encoding="utf-8")
    ledger = IndexLedger(tmp_path / "l.json")
    assert ledger.import_from(src) == 1
    with pytest.raises(LedgerError):
        ledger.import_from(src)
