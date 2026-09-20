from app.models import GraphExpansionRow
from app.services.company_chat import (
    NEWS_KEYWORD,
    WRITE_CLAUSE,
    _build_context_text,
    _distinct,
    _format_graph_expansion_rows,
    _normalize_provider,
)
from app.vector.document import Document


def test_normalize_provider():
    assert _normalize_provider("cloud") == "gemini"
    assert _normalize_provider("gemini") == "gemini"
    assert _normalize_provider("claude") == "claude"
    assert _normalize_provider(None) == "local"
    assert _normalize_provider("무언가") == "local"


def test_write_clause_blocks_mutations():
    assert WRITE_CLAUSE.search("MATCH (n) DETACH delete n")
    assert WRITE_CLAUSE.search("merge (n:Company)")
    assert not WRITE_CLAUSE.search("MATCH (f:Filer) RETURN count(f) ORDER BY f.name LIMIT 5")


def test_news_keyword_detection():
    assert NEWS_KEYWORD.search("F&F 관련 뉴스 3개만 알려줘")
    assert not NEWS_KEYWORD.search("F&F 매출액 알려줘")


def test_distinct_drops_blanks_and_duplicates():
    assert _distinct(["a", "a", "", None, "None", "b"]) == ["a", "b"]


def test_context_text_numbers_sources():
    docs = [
        Document(text="본문A", metadata={"corp_name": "F&F", "report_nm": "사업보고서", "section_title": "개요"}),
        Document(text="본문B", metadata={"corp_name": "F&F", "report_nm": "분기보고서", "section_title": "재무"}),
    ]
    text = _build_context_text(docs)
    assert "[1] (출처: F&F 사업보고서 > 개요) 본문A" in text
    assert "[2] (출처: F&F 분기보고서 > 재무) 본문B" in text


def test_graph_expansion_formatting():
    rows = [
        GraphExpansionRow(
            kind="DISCLOSURE", anchor="1", primaryName="홍길동", secondaryName="대량보유상황보고서",
            relatedCorpCode="00126380", relatedCorpName="삼성전자",
        ),
        GraphExpansionRow(
            kind="NEWS", anchor="u", primaryName="한국경제", secondaryName="기사제목",
            relatedCorpCode="00164779", relatedCorpName="SK하이닉스",
        ),
    ]
    text = _format_graph_expansion_rows(rows)
    assert "[지분공시] 제출인 홍길동이(가) 공시한 \"대량보유상황보고서\"" in text
    assert "다른 기업: 삼성전자 (00126380)" in text
    assert "[뉴스] \"기사제목\" (한국경제) 기사는 SK하이닉스에 대해서도 다룸" in text
