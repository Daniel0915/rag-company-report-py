from __future__ import annotations

import json
import logging

import pytest

from app.dart.dto import DisclosureItem
from app.dart.errors import DartApiError
from app.ingest.ledger import IndexLedger
from app.services import company_report_index as mod
from app.services.company_report_index import (
    CompanyReportIndexService,
    correction_base_name,
)
from app.watchlist import WatchedCompany

COMPANY = WatchedCompany(name="테스트", corp_code="00000001", stock_code="000001")

XML = """<?xml version="1.0"?>
<DOCUMENT>
<TITLE ATOC="Y">1. 회사의 개요</TITLE>
<P>회사는 반도체를 만든다.</P>
</DOCUMENT>
""".encode("utf-8")


class FakeDart:
    def __init__(self, files=None):
        self.files = files or {}
        self.fetched: list[str] = []

    def fetch_document(self, rcept_no):
        self.fetched.append(rcept_no)
        return self.files


class FakeStore:
    def __init__(self, fail_on_add_call=None):
        self.added = []
        self.deleted = []
        self.add_calls = 0
        self.fail_on_add_call = fail_on_add_call

    def add(self, chunks):
        self.add_calls += 1
        if self.add_calls == self.fail_on_add_call:
            raise RuntimeError("add failed")
        self.added.extend(chunks)

    def delete(self, ids):
        self.deleted.extend(ids)


class FakeRegistry:
    def __init__(self):
        self.store = FakeStore()

    def for_company(self, corp_code):
        return self.store


class FakeGraph:
    def __init__(self, original=None):
        self.original = original
        self.recorded = []
        self.corrections = []

    def record_disclosure(self, company, item, pblntf_ty):
        self.recorded.append(item.rcept_no)

    def record_correction(self, corp_code, rcept_no, base_name):
        self.corrections.append((corp_code, rcept_no, base_name))
        return self.original


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)


def _service(tmp_path, files=None, original=None):
    path = tmp_path / "index.json"
    ledger = IndexLedger(path)
    dart, registry, graph = FakeDart(files), FakeRegistry(), FakeGraph(original)
    return CompanyReportIndexService(dart, registry, graph, ledger), dart, registry, graph, ledger, path


def _item(rcept_no, report_nm="사업보고서 (2024.12)"):
    return DisclosureItem(rcept_no=rcept_no, report_nm=report_nm, rcept_dt="20250101")


def test_already_recorded_disclosure_is_not_downloaded(tmp_path):
    service, dart, registry, graph, ledger, _ = _service(tmp_path)
    ledger.put("R1/R1.xml", "h", ["c1"])
    result = service.index_disclosure(COMPANY, _item("R1"), "A")
    assert result.status == "skipped"
    assert result.chunks == 0
    assert dart.fetched == []
    assert registry.store.added == []
    assert graph.recorded == ["R1"]


def test_new_disclosure_is_chunked_and_recorded(tmp_path):
    service, dart, registry, _, ledger, path = _service(tmp_path, {"R2.xml": XML})
    result = service.index_disclosure(COMPANY, _item("R2"), "I")
    assert result.status == "new"
    assert result.chunks >= 1
    assert ledger.has_rcept("R2")
    assert "R2/R2.xml" in json.loads(path.read_text(encoding="utf-8"))
    assert all(c.metadata["pblntf_ty"] == "I" for c in registry.store.added)
    assert all("corrects_rcept_no" not in c.metadata for c in registry.store.added)


def test_zip_without_xml_is_recorded_as_processed(tmp_path):
    service, _, registry, _, ledger, path = _service(tmp_path, {"R3.pdf": b"%PDF"})
    result = service.index_disclosure(COMPANY, _item("R3"), "I")
    assert result.status == "empty"
    assert ledger.has_rcept("R3")
    assert registry.store.added == []
    assert "R3/-" in json.loads(path.read_text(encoding="utf-8"))


def test_correction_links_original(tmp_path):
    service, _, registry, graph, _, _ = _service(tmp_path, {"R4.xml": XML}, original="R0")
    service.index_disclosure(COMPANY, _item("R4", "[기재정정]사업보고서 (2024.12)"), "A")
    assert graph.corrections == [("00000001", "R4", "사업보고서 (2024.12)")]
    assert registry.store.added
    assert all(c.metadata["corrects_rcept_no"] == "R0" for c in registry.store.added)


def test_correction_base_name():
    assert correction_base_name("[기재정정]사업보고서 (2024.12)") == "사업보고서 (2024.12)"
    assert correction_base_name("[첨부정정] 영업(잠정)실적(공정공시)") == "영업(잠정)실적(공정공시)"
    assert correction_base_name("사업보고서 (2024.12)") is None


def test_failure_midway_rolls_back_and_leaves_no_ledger_key(tmp_path):
    xml_b = XML.replace("반도체".encode(), "배터리".encode())
    service, dart, registry, _, _, path = _service(
        tmp_path, {"A.xml": XML, "B.xml": xml_b}
    )
    registry.store.fail_on_add_call = 2
    with pytest.raises(RuntimeError):
        service.index_disclosure(COMPANY, _item("R9"), "A")

    assert not IndexLedger(path).has_rcept("R9")
    first_ids = [c.id for c in registry.store.added]
    assert first_ids and set(first_ids) <= set(registry.store.deleted)
    # B.xml 청크 id도 add 전에 만들어졌으므로 함께 지워진다.
    assert len(registry.store.deleted) > len(first_ids)

    registry.store.fail_on_add_call = None
    result = service.index_disclosure(COMPANY, _item("R9"), "A")
    assert dart.fetched == ["R9", "R9"]
    assert result.status == "new"
    assert IndexLedger(path).has_rcept("R9")


class MissingDocumentDart(FakeDart):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def fetch_document(self, rcept_no):
        self.fetched.append(rcept_no)
        raise DartApiError(self.status, "파일이 존재하지 않습니다.")


def test_missing_document_is_recorded_as_processed(tmp_path, caplog):
    service, _, registry, _, ledger, path = _service(tmp_path)
    service._dart = MissingDocumentDart("014")
    with caplog.at_level(logging.WARNING, logger="app.ingest"):
        result = service.index_disclosure(COMPANY, _item("R5"), "A")
    assert result.status == "empty"
    assert IndexLedger(path).has_rcept("R5")
    assert registry.store.added == []
    assert any("R5" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_other_dart_errors_still_fail(tmp_path):
    service, _, _, _, ledger, _ = _service(tmp_path)
    service._dart = MissingDocumentDart("020")
    with pytest.raises(DartApiError):
        service.index_disclosure(COMPANY, _item("R6"), "A")
    assert not ledger.has_rcept("R6")
