"""
워치리스트 기업의 정기공시(사업/반기/분기보고서)와 지분공시(대량보유상황보고서,
임원ㆍ주요주주소유보고서 등)를 DART에서 받아 목차 단위(DartXmlDocumentReader)로 쪼갠 뒤
기업별 벡터 인덱스에 색인한다. rcept_no+파일명 단위 해시로 이미 색인된 문서는
재다운로드/재색인하지 않는다(DART API 일일 호출 제한 때문에 같은 문서를 반복해서 받지 않도록).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.dart.client import DartApiClient
from app.dart.dto import DisclosureItem
from app.dart.xml_reader import DartXmlDocumentReader
from app.models import IndexResult
from app.services.disclosure_graph import DisclosureGraphService
from app.vector.registry import CompanyVectorStoreRegistry
from app.vector.splitter import TokenTextSplitter
from app.watchlist import WATCHLIST, WatchedCompany

# 색인 대상 공시유형(pblntf_ty). A=정기공시, D=지분공시.
DISCLOSURE_TYPES = ["A", "D"]
PAGE_SIZE = 100
# opendart.fss.or.kr는 짧은 시간에 요청이 몰리면 커넥션을 끊는 방식으로 막는다 - 호출 사이에 쉬어간다.
REQUEST_INTERVAL_SECONDS = 0.3


@dataclass
class _DocRecord:
    hash: str
    chunkIds: list[str]


class CompanyReportIndexService:
    def __init__(
        self,
        dart_client: DartApiClient,
        registry: CompanyVectorStoreRegistry,
        graph_service: DisclosureGraphService,
        sidecar_path: str,
    ) -> None:
        self._dart = dart_client
        self._registry = registry
        self._graph = graph_service
        self._splitter = TokenTextSplitter()
        self._sidecar_file = Path(sidecar_path)
        self._sidecar_file.parent.mkdir(parents=True, exist_ok=True)
        self._sidecar = self._load_sidecar()
        self._lock = threading.Lock()

    def index_watched_companies(self, bgn_de: date, end_de: date) -> list[IndexResult]:
        """워치리스트 전체 기업의 기간 내 정기공시+지분공시를 색인한다."""
        with self._lock:
            results: list[IndexResult] = []
            for company in WATCHLIST:
                results.extend(self._index_company(company, bgn_de, end_de))
            return results

    def _index_company(
        self, company: WatchedCompany, bgn_de: date, end_de: date
    ) -> list[IndexResult]:
        results: list[IndexResult] = []
        for pblntf_ty in DISCLOSURE_TYPES:
            for item in self._search_all_pages(company.corp_code, bgn_de, end_de, pblntf_ty):
                results.append(self._index_disclosure(company, item, pblntf_ty))
        return results

    def _search_all_pages(
        self, corp_code: str, bgn_de: date, end_de: date, pblntf_ty: str
    ) -> list[DisclosureItem]:
        """total_page까지 전부 순회해서 기간 내 해당 공시유형의 모든 공시를 모은다."""
        all_items: list[DisclosureItem] = []
        page_no = 1
        while True:
            response = self._dart.search_disclosures(
                corp_code, bgn_de, end_de, pblntf_ty, page_no, PAGE_SIZE
            )
            time.sleep(REQUEST_INTERVAL_SECONDS)
            if not response.has_data:
                break
            all_items.extend(response.items)
            if page_no >= (response.total_page or 1):
                break
            page_no += 1
        return all_items

    def _index_disclosure(
        self, company: WatchedCompany, item: DisclosureItem, pblntf_ty: str
    ) -> IndexResult:
        self._graph.record_disclosure(company, item, pblntf_ty)
        vector_store = self._registry.for_company(company.corp_code)

        files = self._dart.fetch_document(item.rcept_no)
        time.sleep(REQUEST_INTERVAL_SECONDS)

        total_chunks = 0
        any_new = False
        any_updated = False

        for source_file, xml_bytes in files.items():
            sidecar_key = f"{item.rcept_no}/{source_file}"
            digest = hashlib.md5(xml_bytes).hexdigest()

            existing = self._sidecar.get(sidecar_key)
            if existing is not None and existing.hash == digest:
                continue

            sections = DartXmlDocumentReader(xml_bytes, source_file).read()
            tag_meta = {
                "corp_name": company.name,
                "corp_code": company.corp_code,
                "stock_code": company.stock_code,
                "rcept_no": item.rcept_no,
                "report_nm": item.report_nm,
                "rcept_dt": item.rcept_dt,
                "pblntf_ty": pblntf_ty,
            }
            chunks = self._splitter.apply([d.with_metadata(tag_meta) for d in sections])

            if existing is not None:
                vector_store.delete(existing.chunkIds)
                any_updated = True
            else:
                any_new = True
            vector_store.add(chunks)

            self._sidecar[sidecar_key] = _DocRecord(digest, [c.id for c in chunks])
            total_chunks += len(chunks)

        if total_chunks > 0:
            self._persist_sidecar()

        status = "new" if any_new else ("updated" if any_updated else "skipped")
        return IndexResult(
            corpName=company.name,
            rceptNo=item.rcept_no,
            reportNm=item.report_nm,
            status=status,
            chunks=total_chunks,
        )

    def _load_sidecar(self) -> dict[str, _DocRecord]:
        if not self._sidecar_file.exists():
            return {}
        try:
            raw = json.loads(self._sidecar_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            key: _DocRecord(value.get("hash", ""), value.get("chunkIds", []))
            for key, value in raw.items()
        }

    def _persist_sidecar(self) -> None:
        payload = {
            key: {"hash": record.hash, "chunkIds": record.chunkIds}
            for key, record in self._sidecar.items()
        }
        try:
            self._sidecar_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            raise RuntimeError("색인 사이드카 저장 실패") from e
