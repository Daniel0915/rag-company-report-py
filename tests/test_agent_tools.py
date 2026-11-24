from app.agent.tavily import TavilyError, WebResult
from app.agent.tools import MAX_RESULT_CHARS, TOOL_NAMES, ToolContext, ToolResult, build_tools
from app.vector.document import Document

CORP = "00126380"


def _doc(i: int) -> Document:
    d = Document(
        text=f"본문 {i}",
        metadata={
            "corp_name": "삼성전자",
            "report_nm": "사업보고서",
            "rcept_no": f"2024{i:04d}",
            "source_file": "a.xml",
            "section_order": i,
            "section_title": f"섹션 {i}",
            "doc_type": "REPORT",
        },
    )
    d.score = 0.9
    return d


class FakeChat:
    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def retrieve(self, question, corp_code, top_k, news_query=None, doc_type=None):
        self.calls.append((question, corp_code, top_k, doc_type))
        return self.docs


class FakeGraph:
    def __init__(self, result="- n=1"):
        self.result = result
        self.queries = []

    def run_read_only_query(self, cypher):
        self.queries.append(cypher)
        return self.result


class FakeDart:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_key_accounts(self, corp_code, year, reprt_code):
        self.calls.append((corp_code, year, reprt_code))
        return self.rows


class FakeTavily:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error

    def search(self, query, max_results=5):
        if self.error:
            raise self.error
        return self.results


class FakeSession:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self.log.append((cypher, params))
        return self.rows


class FakeDriver:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.log = []

    def session(self, **kw):
        assert kw.get("default_access_mode") == "READ"
        return FakeSession(self.rows, self.log)


def fake_ctx(**over):
    base = dict(
        corp_code=CORP,
        chat=FakeChat([_doc(1), _doc(2)]),
        graph=FakeGraph(),
        dart=FakeDart([]),
        tavily=FakeTavily(),
        driver=FakeDriver(),
    )
    base.update(over)
    return ToolContext(**base)


def _tool(ctx, name):
    return next(s for s in build_tools(ctx) if s.name == name)


def test_build_tools_names_and_schemas():
    specs = build_tools(fake_ctx())
    assert tuple(s.name for s in specs) == TOOL_NAMES
    assert all(s.input_schema["additionalProperties"] is False for s in specs)
    graph_desc = _tool(fake_ctx(), "query_ownership_graph").description
    assert CORP in graph_desc and "(:Report" in graph_desc and "MATCH" in graph_desc


def test_search_disclosures_returns_chunk_sources():
    ctx = fake_ctx()
    r = _tool(ctx, "search_disclosures").run({"query": "매출", "doc_type": "NEWS"})
    assert r.ok and len(r.sources) == 2
    assert r.sources[0]["kind"] == "chunk" and r.sources[1]["section_order"] == 2
    assert r.sources[0]["text"] == "본문 1" and r.sources[0]["score"] == 0.9
    assert ctx.chat.calls == [("매출", CORP, 6, "NEWS")]


def test_search_disclosures_reports_no_results():
    r = _tool(fake_ctx(chat=FakeChat([])), "search_disclosures").run({"query": "x"})
    assert r.ok and "결과 없음" in r.text and r.sources == []


def test_tool_rejects_corp_outside_watchlist():
    r = _tool(fake_ctx(), "search_disclosures").run({"query": "x", "corp_code": "99999999"})
    assert not r.ok and "워치리스트에 없는 기업 코드" in r.text


def test_query_ownership_graph_blocks_write_and_empty():
    t = _tool(fake_ctx(), "query_ownership_graph")
    assert not t.run({"cypher": "MATCH (n) DETACH DELETE n"}).ok
    r = _tool(fake_ctx(graph=FakeGraph(None)), "query_ownership_graph").run(
        {"cypher": "MATCH (n) RETURN n LIMIT 1"}
    )
    assert r.ok and "결과 없음" in r.text


def test_query_ownership_graph_returns_rows():
    r = _tool(fake_ctx(), "query_ownership_graph").run({"cypher": "MATCH (n) RETURN n"})
    assert r.ok and r.text == "- n=1"


def test_web_search_sources_and_tavily_error():
    tav = FakeTavily([WebResult("제목", "https://x.test", "요약")])
    r = _tool(fake_ctx(tavily=tav), "web_search").run({"query": "q"})
    assert r.ok and r.sources == [{"kind": "web", "title": "제목", "url": "https://x.test"}]
    assert "https://x.test" in r.text
    bad = _tool(fake_ctx(tavily=FakeTavily(error=TavilyError("x"))), "web_search").run({"query": "q"})
    assert not bad.ok


def test_get_financials_prefers_consolidated():
    rows = [
        {"account_nm": "매출액", "fs_div": "OFS", "thstrm_amount": "1", "frmtrm_amount": "2"},
        {"account_nm": "매출액", "fs_div": "CFS", "thstrm_amount": "300", "frmtrm_amount": "200"},
    ]
    ctx = fake_ctx(dart=FakeDart(rows))
    r = _tool(ctx, "get_financials").run({"year": 2024, "report": "annual"})
    assert r.ok and r.text.splitlines()[-1] == "매출액: 300 (200)"
    assert "1 (2)" not in r.text
    assert ctx.dart.calls == [(CORP, 2024, "11011")]


def test_get_financials_falls_back_to_separate_and_empty():
    rows = [{"account_nm": "매출액", "fs_div": "OFS", "thstrm_amount": "1", "frmtrm_amount": "2"}]
    r = _tool(fake_ctx(dart=FakeDart(rows)), "get_financials").run({"year": 2024, "report": "q3"})
    assert "매출액: 1 (2)" in r.text
    r = _tool(fake_ctx(), "get_financials").run({"year": 2024, "report": "half"})
    assert r.ok and "결과 없음" in r.text


def test_list_recent_disclosures_formats_rows_and_filters():
    rows = [{"rcept_dt": "20241105", "pblntf_ty": "B", "report_nm": "주요사항보고서", "rcept_no": "2024110500"}]
    ctx = fake_ctx(driver=FakeDriver(rows))
    r = _tool(ctx, "list_recent_disclosures").run(
        {"since": "2024-11-01", "types": ["B"], "limit": 5}
    )
    assert r.ok and r.text == "2024-11-05 B 주요사항보고서 (2024110500)"
    cypher, params = ctx.driver.log[0]
    assert params["since"] == "20241101" and params["types"] == ["B"] and params["limit"] == 5
    assert params["corp_code"] == CORP and "ORDER BY r.rcept_dt DESC" in cypher


def test_list_recent_disclosures_bad_date_and_empty():
    t = _tool(fake_ctx(), "list_recent_disclosures")
    assert not t.run({"since": "11/01"}).ok
    assert "결과 없음" in t.run({}).text


def test_calculate_tool():
    t = _tool(fake_ctx(), "calculate")
    assert t.run({"expression": "1+2"}).text == "3"
    assert t.run({"expression": "10/4"}).text == "2.5"
    assert not t.run({"expression": "2**3"}).ok


def test_long_result_is_truncated():
    ctx = fake_ctx(graph=FakeGraph("가" * 10000))
    r = _tool(ctx, "query_ownership_graph").run({"cypher": "MATCH (n) RETURN n"})
    assert len(r.text) <= MAX_RESULT_CHARS + len("…(생략)") and r.text.endswith("…(생략)")


def test_tool_result_of_truncates():
    assert ToolResult.of(True, "a" * 5000).text.endswith("…(생략)")


def test_calculate_tool_never_raises_on_extreme_input():
    t = _tool(fake_ctx(), "calculate")
    for expr in ["*".join(["9" * 200] * 6), "(" * 500 + "1" + ")" * 500, "1e308*10", "1e308*10-1e308*10"]:
        assert not t.run({"expression": expr}).ok


def test_get_financials_returns_failure_on_http_and_parse_errors():
    import httpx

    for exc in (httpx.ConnectError("secret"), ValueError("secret")):

        class Boom:
            def fetch_key_accounts(self, *a):
                raise exc

        r = _tool(fake_ctx(dart=Boom()), "get_financials").run({"year": 2024, "report": "annual"})
        assert not r.ok and "secret" not in r.text
