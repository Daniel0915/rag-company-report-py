"""
DART Open API(https://opendart.fss.or.kr) 클라이언트.
공시검색(list.json), 공시서류원본파일(document.xml), 고유번호(corpCode.xml)만 다룬다.
pdf.do는 브라우저 세션 기반 비공개 엔드포인트라 API로는 안정적으로 못 받으므로 다루지 않음.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from datetime import date
from typing import Any, Callable, TypeVar
from xml.etree import ElementTree

import httpx

from app.config import Settings
from app.dart.dto import CorpCode, DartListResponse
from app.dart.errors import DartApiError

T = TypeVar("T")

_STATUS_PATTERN = re.compile(r"<status>([^<]*)</status>")
_MESSAGE_PATTERN = re.compile(r"<message>([^<]*)</message>")

# opendart.fss.or.kr가 간헐적으로 connection reset을 던지는 경우가 있어 재시도로 흡수한다.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5


class DartApiClient:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.dart_api_key
        # document.xml/corpCode.xml은 수십 MB라 넉넉한 read timeout이 필요하다.
        self._client = httpx.Client(
            base_url=settings.dart_base_url,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def search_disclosures(
        self,
        corp_code: str,
        bgn_de: date,
        end_de: date,
        pblntf_ty: str | None = None,
        page_no: int = 1,
        page_count: int = 100,
    ) -> DartListResponse:
        """공시검색. pblntf_ty(공시유형)로 좁혀서 조회한다. "A"=정기공시, "D"=지분공시. None이면 전체."""
        params: dict[str, Any] = {
            "crtfc_key": self._api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de.strftime("%Y%m%d"),
            "end_de": end_de.strftime("%Y%m%d"),
            "page_no": page_no,
            "page_count": page_count,
        }
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty

        raw = self._with_retry(lambda: self._client.get("/list.json", params=params).json())
        if raw is None:
            raise DartApiError("UNKNOWN", "list.json 응답이 비어 있습니다")

        response = DartListResponse.from_json(raw)
        if not response.has_data and not response.no_data:
            raise DartApiError(response.status, response.message)
        return response

    def fetch_document(self, rcept_no: str) -> dict[str, bytes]:
        """공시서류원본파일: rcept_no의 원본 zip을 받아 파일명별 바이트로 풀어서 반환한다."""
        body = self._with_retry(
            lambda: self._client.get(
                "/document.xml", params={"crtfc_key": self._api_key, "rcept_no": rcept_no}
            ).content
        )
        self._require_zip(body)
        return self._unzip_all(body)

    def fetch_corp_codes(self) -> list[CorpCode]:
        """고유번호: 전체 기업의 corp_code ↔ 기업명/종목코드 매핑 (약 30MB, 자주 호출할 필요는 없음)."""
        body = self._with_retry(
            lambda: self._client.get("/corpCode.xml", params={"crtfc_key": self._api_key}).content
        )
        self._require_zip(body)
        entries = self._unzip_all(body)
        xml = entries.get("CORPCODE.xml") or next(iter(entries.values()))
        return self._parse_corp_code_xml(xml)

    def _with_retry(self, call: Callable[[], T]) -> T:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return call()
            except (httpx.TransportError, httpx.HTTPError):
                if attempt == MAX_ATTEMPTS:
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        raise RuntimeError("unreachable")

    def _require_zip(self, body: bytes | None) -> None:
        if not body:
            raise DartApiError("UNKNOWN", "응답이 비어 있습니다")
        if not body.startswith(b"PK"):
            raise self._parse_error_xml(body)

    @staticmethod
    def _parse_error_xml(body: bytes) -> DartApiError:
        text = body.decode("utf-8", errors="replace")
        status = _STATUS_PATTERN.search(text)
        message = _MESSAGE_PATTERN.search(text)
        return DartApiError(
            status.group(1) if status else "UNKNOWN",
            message.group(1) if message else text,
        )

    @staticmethod
    def _unzip_all(zip_bytes: bytes) -> dict[str, bytes]:
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                return {info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()}
        except zipfile.BadZipFile as e:
            raise RuntimeError("공시서류 zip 압축 해제 실패") from e

    @staticmethod
    def _parse_corp_code_xml(xml_bytes: bytes) -> list[CorpCode]:
        def text(node: ElementTree.Element, tag: str) -> str | None:
            child = node.find(tag)
            return child.text.strip() if child is not None and child.text else None

        try:
            root = ElementTree.fromstring(xml_bytes)
        except ElementTree.ParseError as e:
            raise RuntimeError("CORPCODE.xml 파싱 실패") from e

        result: list[CorpCode] = []
        for node in root.iter("list"):
            corp_code = text(node, "corp_code")
            if not corp_code:
                continue
            result.append(
                CorpCode(
                    corp_code=corp_code,
                    corp_name=text(node, "corp_name"),
                    corp_eng_name=text(node, "corp_eng_name"),
                    stock_code=text(node, "stock_code"),
                    modify_date=text(node, "modify_date"),
                )
            )
        return result
