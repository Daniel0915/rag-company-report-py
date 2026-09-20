from app.dart.xml_reader import DartXmlDocumentReader

SAMPLE = """<?xml version="1.0"?>
<DOCUMENT>
<!-- 주석은 제거된다 -->
<COVER>표지 내용</COVER>
<TITLE ATOC="Y" AASSOCNOTE="D-0">I. 회사의 개요</TITLE>
<TITLE ATOC="Y">1. 회사의 개요</TITLE>
<P>회사는 반도체를 만든다. S&amp;P 등급도 있다.</P>
<TABLE>
<TR><TD>항목</TD><TD>금액</TD></TR>
<TR><TD>매출</TD><TD>100</TD></TR>
</TABLE>
<TITLE ATOC="N">목차에 없는 제목</TITLE>
<TITLE ATOC="Y">2. 회사의 연혁</TITLE>
<P>1969년 설립</P>
</DOCUMENT>
"""


def _read():
    return DartXmlDocumentReader(SAMPLE.encode("utf-8"), "20240101000001.xml").read()


def test_splits_by_toc_headings():
    titles = [d.metadata["section_title"] for d in _read()]
    # 본문 없이 곧바로 하위 헤딩이 오는 "I. 회사의 개요"는 빈 청크라 제외된다.
    assert titles == ["표지", "1. 회사의 개요", "2. 회사의 연혁"]


def test_flattens_table_rows_and_unescapes_entities():
    body = next(d for d in _read() if d.metadata["section_title"] == "1. 회사의 개요").text
    assert "S&P 등급도 있다." in body
    assert "항목 | 금액" in body
    assert "매출 | 100" in body


def test_heading_level_and_order_metadata():
    docs = _read()
    assert docs[0].metadata["section_order"] == 0
    assert docs[1].metadata["section_level"] == 2  # "1." 로 시작 → 2레벨
    assert all(d.metadata["source_file"] == "20240101000001.xml" for d in docs)
