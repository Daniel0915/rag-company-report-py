"""
DART 공시서류 원본(document.xml 안의 XML)을 목차(TITLE ATOC="Y") 단위로 쪼개서 Document로 만든다.
DART XML은 완전한 well-formed XML이 아니라서(예: "S&P" 처럼 이스케이프 안 된 &)
표준 XML 파서 대신 정규식으로 태그를 다룬다.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.vector.document import Document

_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_TR_PATTERN = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.DOTALL | re.IGNORECASE)
_CELL_PATTERN = re.compile(r"<(?:TD|TH|TE|TU)\b[^>]*>(.*?)</(?:TD|TH|TE|TU)>", re.DOTALL | re.IGNORECASE)
_TITLE_PATTERN = re.compile(r"<TITLE\b([^>]*)>(.*?)</TITLE>", re.DOTALL | re.IGNORECASE)
_ATOC_PATTERN = re.compile(r'ATOC="([^"]*)"', re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")

_ROMAN_HEADING = re.compile(r"[IVXLCM]+[.가-힣]")
_SUB_NUMBER_HEADING = re.compile(r"\d+-\d+[.)]")
_NUMBER_HEADING = re.compile(r"\d+[.)]")
_HANGUL_ORDINAL_HEADING = re.compile(r"[가-힣][.)]")
_PAREN_NUMBER_HEADING = re.compile(r"\(\d+\)")


@dataclass(frozen=True)
class _Heading:
    tag_start: int
    tag_end: int
    level: int
    title: str


class DartXmlDocumentReader:
    def __init__(self, xml_bytes: bytes, source_file: str) -> None:
        """
        :param xml_bytes: document.xml zip 안에 들어있던 개별 xml 파일의 원본 바이트
        :param source_file: 원본 파일명 (예: "20260814003699.xml")
        """
        self._xml_bytes = xml_bytes
        self._source_file = source_file

    def read(self) -> list[Document]:
        content = self._xml_bytes.decode("utf-8", errors="replace")
        content = _COMMENT_PATTERN.sub("", content)
        content = _flatten_tables(content)

        headings = _find_headings(content)
        documents: list[Document] = []
        order = 0

        first_start = headings[0].tag_start if headings else len(content)
        cover_body = _strip_tags(content[:first_start])
        if cover_body.strip():
            documents.append(self._build("표지", 0, order, cover_body))
            order += 1

        for i, heading in enumerate(headings):
            body_end = headings[i + 1].tag_start if i + 1 < len(headings) else len(content)
            body = _strip_tags(content[heading.tag_end : body_end])
            # 본문 없이 곧바로 하위 헤딩으로 이어지는 상위 목차(예: "II. 사업의 내용")는
            # 제목만 있는 빈 청크가 되어 실제 내용 청크보다 검색 유사도가 더 높게 나오므로 건너뛴다.
            if not body.strip():
                continue
            documents.append(self._build(heading.title, heading.level, order, f"{heading.title}\n\n{body}"))
            order += 1

        return documents

    def _build(self, section_title: str, level: int, order: int, text: str) -> Document:
        return Document(
            text=text,
            metadata={
                "source_file": self._source_file,
                "section_title": section_title,
                "section_level": level,
                "section_order": order,
            },
        )


def _find_headings(content: str) -> list[_Heading]:
    headings: list[_Heading] = []
    for match in _TITLE_PATTERN.finditer(content):
        atoc_match = _ATOC_PATTERN.search(match.group(1))
        atoc = atoc_match.group(1) if atoc_match else "N"
        title = _clean_inline(match.group(2))
        if atoc == "Y" and title:
            headings.append(_Heading(match.start(), match.end(), _heading_level(title), title))
    return headings


def _heading_level(text: str) -> int:
    t = text.strip()
    if t.startswith("【"):
        return 1
    if _ROMAN_HEADING.match(t):
        return 1
    if _SUB_NUMBER_HEADING.match(t):
        return 3
    if _NUMBER_HEADING.match(t):
        return 2
    if _HANGUL_ORDINAL_HEADING.match(t):
        return 4
    if _PAREN_NUMBER_HEADING.match(t):
        return 5
    return 3


def _flatten_tables(content: str) -> str:
    """표는 행 단위로 " | " 로 이어붙여 한 줄 텍스트로 편다."""

    def replace(match: re.Match[str]) -> str:
        cells = [
            cell
            for cell in (_clean_inline(m.group(1)) for m in _CELL_PATTERN.finditer(match.group(1)))
            if cell
        ]
        return "\n" + " | ".join(cells) + "\n" if cells else ""

    return _TR_PATTERN.sub(replace, content)


def _clean_inline(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_PATTERN.sub(" ", text))).strip()


def _strip_tags(text: str) -> str:
    unescaped = html.unescape(_TAG_PATTERN.sub("\n", text))
    return "\n".join(line.strip() for line in unescaped.split("\n") if line.strip())
