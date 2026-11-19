"""
워치리스트 기업의 모든 유형(A~J) DART 공시를 "공시 1건" 단위로 색인한다.
공시를 목차 단위(DartXmlDocumentReader)로 쪼갠 뒤 기업별 벡터 인덱스에 넣는다.

이미 색인한 공시는 다운로드 전에 건너뛴다: IndexLedger에 그 접수번호의 키가 하나라도 있으면
fetch_document를 부르지 않는다(DART 일일 호출 한도 때문에, 그리고 같은 공시의 청크가
Neo4j에 중복으로 쌓이지 않도록). 어떤 공시를 언제 돌릴지(기간·재시도·잠금)는 호출자(IngestRunner)의 몫이다.
정정 공시("[기재정정]..." 등)는 원본 공시와 CORRECTS 관계로 잇고 청크 메타데이터에 원본 접수번호를 남긴다.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import date

from app.dart.client import DartApiClient
from app.dart.dto import DisclosureItem
from app.dart.xml_reader import DartXmlDocumentReader
from app.ingest.ledger import IndexLedger
from app.models import IndexResult
from app.services.disclosure_graph import DisclosureGraphService
from app.vector.registry import CompanyVectorStoreRegistry
from app.vector.splitter import TokenTextSplitter
from app.watchlist import WatchedCompany

# 수집 대상 공시유형(pblntf_ty) 전부. A=정기, B=주요사항, C=발행, D=지분, E=기타, F=외부감사,
# G=펀드, H=자산유동화, I=거래소, J=공정위.
ALL_DISCLOSURE_TYPES = list("ABCDEFGHIJ")
PAGE_SIZE = 100
# opendart.fss.or.kr는 짧은 시간에 요청이 몰리면 커넥션을 끊는 방식으로 막는다 - 호출 사이에 쉬어간다.
REQUEST_INTERVAL_SECONDS = 0.3

_CORRECTION_PREFIX = re.compile(r"^\[[^\]]+\]\s*")


def correction_base_name(report_nm: str) -> str | None:
    """"[기재정정]사업보고서"처럼 정정 접두어가 있으면 뗀 이름, 없으면 None."""
    match = _CORRECTION_PREFIX.match(report_nm)
    return report_nm[match.end():] if match else None


class CompanyReportIndexService:
    def __init__(
        self,
        dart_client: DartApiClient,
        registry: CompanyVectorStoreRegistry,
        graph_service: DisclosureGraphService,
        ledger: IndexLedger,
    ) -> None:
        self._dart = dart_client
        self._registry = registry
        self._graph = graph_service
        self._splitter = TokenTextSplitter()
        self._ledger = ledger

    def list_disclosures(
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

    def index_disclosure(
        self, company: WatchedCompany, item: DisclosureItem, pblntf_ty: str
    ) -> IndexResult:
        self._graph.record_disclosure(company, item, pblntf_ty)

        # 다운로드 전에 건너뛴다 - 이미 색인한 공시를 다시 받으면 청크가 중복된다.
        if self._ledger.has_rcept(item.rcept_no):
            return self._result(company, item, "skipped", 0)

        tag_meta = {
            "corp_name": company.name,
            "corp_code": company.corp_code,
            "stock_code": company.stock_code,
            "rcept_no": item.rcept_no,
            "report_nm": item.report_nm,
            "rcept_dt": item.rcept_dt,
            "pblntf_ty": pblntf_ty,
        }
        base_name = correction_base_name(item.report_nm)
        if base_name is not None:
            original = self._graph.record_correction(
                company.corp_code, item.rcept_no, base_name
            )
            if original:
                tag_meta["corrects_rcept_no"] = original

        total_chunks = 0
        # 공시 1건은 하나의 작업 단위다: 모든 파일이 성공해야만 기록 파일에 넣는다.
        # 일부만 기록하면 재시도가 "이미 색인함"으로 건너뛰어 나머지 파일이 영영 빠진다.
        entries: list[tuple[str, str, list[str]]] = []
        produced_ids: list[str] = []
        vector_store = self._registry.for_company(company.corp_code)
        try:
            files = self._dart.fetch_document(item.rcept_no)
            time.sleep(REQUEST_INTERVAL_SECONDS)

            xml_files = {n: b for n, b in files.items() if n.lower().endswith(".xml")}
            for source_file, xml_bytes in xml_files.items():
                sections = DartXmlDocumentReader(xml_bytes, source_file).read()
                chunks = self._splitter.apply([d.with_metadata(tag_meta) for d in sections])
                ids = [c.id for c in chunks]
                # add가 배치(16개) 단위로 쓰다 중간에 실패해도 지울 수 있도록 add 전에 id를 적어 둔다.
                produced_ids.extend(ids)
                vector_store.add(chunks)
                # 청크 0개여도 기록한다 - 다음 실행에서 같은 파일을 다시 받지 않게.
                entries.append(
                    (f"{item.rcept_no}/{source_file}", hashlib.md5(xml_bytes).hexdigest(), ids)
                )
                total_chunks += len(chunks)
        except BaseException:
            # 기록 파일에는 넣지 않으므로, 이번 공시로 넣은 청크는 되돌려 재시도 때 중복되지 않게 한다.
            if produced_ids:
                vector_store.delete(produced_ids)
            raise

        if xml_files:
            for key, digest, ids in entries:
                self._ledger.put(key, digest, ids)
        else:
            self._ledger.mark_processed_without_files(item.rcept_no)
        self._ledger.save()

        return self._result(company, item, "new" if total_chunks > 0 else "empty", total_chunks)

    @staticmethod
    def _result(
        company: WatchedCompany, item: DisclosureItem, status: str, chunks: int
    ) -> IndexResult:
        return IndexResult(
            corpName=company.name,
            rceptNo=item.rcept_no,
            reportNm=item.report_nm,
            status=status,
            chunks=chunks,
        )
