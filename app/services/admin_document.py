"""
어드민 페이지에서 업로드한 PDF를 텍스트 추출 → 청킹 → 태깅해서 벡터스토어에 색인한다.
DART 공시 색인과 동일한 메타데이터 키(corp_code, report_nm, rcept_no, section_title 등)를
사용해서 CompanyChatService가 그대로 근거로 쓸 수 있게 한다.
"""
from __future__ import annotations

import io
import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

from app.models import AdminDocumentDto
from app.vector.document import Document
from app.vector.registry import CompanyVectorStoreRegistry
from app.vector.splitter import TokenTextSplitter
from app.watchlist import find as find_company


@dataclass
class _AdminDocumentRecord:
    id: str
    corpCode: str
    corpName: str
    title: str
    category: str | None
    description: str | None
    docDate: str | None
    fileName: str
    storedFileName: str
    uploadedAt: str
    chunkIds: list[str] = field(default_factory=list)


class AdminDocumentService:
    def __init__(
        self, registry: CompanyVectorStoreRegistry, registry_path: str, upload_dir: str
    ) -> None:
        self._registry = registry
        self._splitter = TokenTextSplitter()
        self._registry_file = Path(registry_path)
        self._registry_file.parent.mkdir(parents=True, exist_ok=True)
        self._upload_dir = Path(upload_dir)
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        self._records = self._load_registry()
        self._lock = threading.Lock()

    def list_documents(self) -> list[AdminDocumentDto]:
        with self._lock:
            ordered = sorted(self._records.values(), key=lambda r: r.uploadedAt, reverse=True)
            return [_to_dto(r) for r in ordered]

    def upload_document(
        self,
        file_bytes: bytes,
        original_filename: str | None,
        corp_code: str,
        title: str | None,
        category: str | None,
        description: str | None,
        doc_date: str | None,
    ) -> AdminDocumentDto:
        if not file_bytes:
            raise ValueError("파일이 비어 있습니다")
        company = find_company(corp_code)
        if company is None:
            raise ValueError(f"워치리스트에 없는 기업입니다: {corp_code}")

        with self._lock:
            doc_id = str(uuid.uuid4())
            filename = original_filename or "document.pdf"
            stored_file_name = f"{doc_id}_{filename}"
            effective_title = title or filename

            (self._upload_dir / stored_file_name).write_bytes(file_bytes)

            pages = _read_pdf_pages(file_bytes, filename)

            tag_meta: dict[str, object] = {
                "doc_type": "ADMIN_PDF",
                "admin_doc_id": doc_id,
                "corp_name": company.name,
                "corp_code": company.corp_code,
                "stock_code": company.stock_code,
                "report_nm": effective_title,
                "rcept_no": f"ADMIN-{doc_id[:8]}",
                "section_title": category or effective_title,
                "source_file": filename,
            }
            if doc_date:
                tag_meta["rcept_dt"] = doc_date
            if description:
                tag_meta["description"] = description

            chunks = self._splitter.apply([p.with_metadata(tag_meta) for p in pages])
            self._registry.for_company(company.corp_code).add(chunks)

            record = _AdminDocumentRecord(
                id=doc_id,
                corpCode=company.corp_code,
                corpName=company.name,
                title=effective_title,
                category=category,
                description=description,
                docDate=doc_date,
                fileName=filename,
                storedFileName=stored_file_name,
                uploadedAt=datetime.now().isoformat(),
                chunkIds=[c.id for c in chunks],
            )
            self._records[doc_id] = record
            self._persist_registry()
            return _to_dto(record)

    def delete_document(self, doc_id: str) -> None:
        with self._lock:
            record = self._records.pop(doc_id, None)
            if record is None:
                raise KeyError(f"문서를 찾을 수 없습니다: {doc_id}")
            if record.chunkIds:
                self._registry.for_company(record.corpCode).delete(record.chunkIds)
            (self._upload_dir / record.storedFileName).unlink(missing_ok=True)
            self._persist_registry()

    def _load_registry(self) -> dict[str, _AdminDocumentRecord]:
        if not self._registry_file.exists():
            return {}
        try:
            raw = json.loads(self._registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {key: _AdminDocumentRecord(**value) for key, value in raw.items()}

    def _persist_registry(self) -> None:
        payload = {key: asdict(record) for key, record in self._records.items()}
        try:
            self._registry_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            raise RuntimeError("문서 등록 정보 저장 실패") from e


def _read_pdf_pages(file_bytes: bytes, filename: str) -> list[Document]:
    """PDF를 페이지 단위 Document로 읽는다(Spring AI PagePdfDocumentReader 대응)."""
    reader = PdfReader(io.BytesIO(file_bytes))
    documents: list[Document] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        documents.append(
            Document(text=text, metadata={"source_file": filename, "page_number": page_number})
        )
    return documents


def _to_dto(record: _AdminDocumentRecord) -> AdminDocumentDto:
    return AdminDocumentDto(
        id=record.id,
        corpCode=record.corpCode,
        corpName=record.corpName,
        title=record.title,
        category=record.category,
        description=record.description,
        docDate=record.docDate,
        fileName=record.fileName,
        uploadedAt=record.uploadedAt,
        chunkCount=len(record.chunkIds),
    )
