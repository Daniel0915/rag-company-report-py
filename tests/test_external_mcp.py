import json
import re

import pytest

from app.agent.external_mcp import (
    EXTERNAL_TOOL_NAMES,
    EXTERNAL_TOOLS,
    ExternalMcpError,
    ToolScope,
    build_external_tools,
    normalize_result,
)
from app.agent.tools import TOOL_NAMES

SCOPE = ToolScope("00126380", "삼성전자", "005930")
KEY = "SECRETKEY123"


def _tool(name):
    return next(t for t in EXTERNAL_TOOLS if t.name == name)


def _spec(caller, name):
    return {s.name: s for s in build_external_tools(caller, SCOPE, [KEY], krx_auth=True)}[name]


def test_external_tool_names_are_fixed_and_valid():
    assert EXTERNAL_TOOL_NAMES == (
        "dart_largest_shareholder",
        "dart_dividend",
        "dart_executives",
        "dart_financial_index",
        "krx_stock_price",
        "krx_market_cap",
        "krx_market_fundamentals",
    )
    assert not set(EXTERNAL_TOOL_NAMES) & set(TOOL_NAMES)
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in EXTERNAL_TOOL_NAMES)


def test_schemas_hide_company_arguments():
    for t in EXTERNAL_TOOLS:
        assert not {"corp_code", "ticker"} & set(t.input_schema["properties"])
        assert t.input_schema["additionalProperties"] is False


def test_dart_arguments_are_converted_and_corp_injected():
    t = _tool("dart_dividend")
    assert t.to_remote({"year": 2025}, SCOPE) == {
        "corp_code": "00126380",
        "bsns_year": "2025",
        "reprt_code": "11011",
    }
    assert t.to_remote({"year": 2025, "report": "half"}, SCOPE)["reprt_code"] == "11012"
    index = _tool("dart_financial_index").to_remote({"year": 2025, "category": "stability"}, SCOPE)
    assert index["idx_cl_code"] == "M220000"


def test_krx_arguments_are_converted_and_ticker_injected():
    remote = _tool("krx_stock_price").to_remote(
        {"start_date": "2026-11-20", "end_date": "2026-11-26"}, SCOPE
    )
    assert remote == {"ticker": "005930", "start_date": "20261120", "end_date": "20261126"}


@pytest.mark.parametrize(
    "name,remote,label",
    [
        ("krx_market_cap", "get_market_cap_by_date", "KRX 시가총액"),
        ("krx_market_fundamentals", "get_market_fundamental_by_date", "KRX 투자지표"),
    ],
)
def test_authenticated_krx_tools_route_and_label_results(name, remote, label):
    args = {"start_date": "2026-11-20", "end_date": "2026-11-20"}

    class Caller:
        def call(self, server, tool, arguments):
            assert server != "pykrx"
            assert tool == remote
            assert arguments == {"ticker": "005930", "start_date": "20261120", "end_date": "20261120"}
            return '{"data":[{"date":"2026-11-20"}]}', False

    assert name not in {spec.name for spec in build_external_tools(None, SCOPE, [])}
    result = _spec(Caller(), name).run(args)
    assert result.ok and result.sources[0]["label"].startswith(label)
    assert result.sources[0]["tool"] == name
    error = normalize_result(_tool(name), args, SCOPE, '{"error":"no data"}', False, [KEY])
    assert not error.ok


@pytest.mark.parametrize(
    "start,end,msg",
    [
        ("2026-01-01", "2026-03-01", "31일"),
        ("20261120", "20261126", "YYYY-MM-DD"),
        ("2026-11-26", "2026-11-20", "늦습니다"),
    ],
)
def test_krx_rejects_bad_ranges(start, end, msg):
    with pytest.raises(ValueError, match=msg):
        _tool("krx_stock_price").to_remote({"start_date": start, "end_date": end}, SCOPE)


def test_dart_success_is_compacted_with_sources():
    raw = json.dumps(
        {
            "status": "000",
            "message": "정상",
            "list": [
                {"rcept_no": "20260310002820", "corp_code": "00126380", "corp_name": "삼성전자", "nm": "홍라희"},
                {"rcept_no": "20260310002820", "corp_code": "00126380", "nm": "이재용"},
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    r = normalize_result(_tool("dart_largest_shareholder"), {"year": 2025}, SCOPE, raw, False, [KEY])
    assert r.ok and "corp_code" not in r.text and "\n" not in r.text and "홍라희" in r.text
    assert r.sources == [
        {
            "kind": "external",
            "server": "opendart",
            "tool": "dart_largest_shareholder",
            "corp_name": "삼성전자",
            "rcept_no": "20260310002820",
            "label": "DART API · 최대주주 현황",
        }
    ]


def test_krx_success_has_single_source():
    args = {"start_date": "2026-11-20", "end_date": "2026-11-26"}
    r = normalize_result(_tool("krx_stock_price"), args, SCOPE, '{"ticker": "005930", "data": []}', False, [KEY])
    assert r.ok and r.sources == [
        {"kind": "external", "server": "pykrx", "tool": "krx_stock_price", "label": "KRX 시세 (pykrx) 2026-11-20~2026-11-26"}
    ]


def test_dart_no_data_status_is_failure():
    r = normalize_result(
        _tool("dart_dividend"), {"year": 2099}, SCOPE,
        '{"status": "013", "message": "조회된 데이타가 없습니다."}', False, [KEY],
    )
    assert not r.ok and "013" in r.text


def test_pykrx_error_key_is_failure():
    r = normalize_result(
        _tool("krx_stock_price"), {"start_date": "2026-11-20", "end_date": "2026-11-26"}, SCOPE,
        '{"error": "No data found", "ticker": "005930"}', False, [KEY],
    )
    assert not r.ok and "No data found" in r.text


def test_error_text_is_redacted():
    text = (
        "Client error '400' for url "
        f"'https://opendart.fss.or.kr/api/alotMatter.json?crtfc_key={KEY}&corp_code=1'"
    )
    r = normalize_result(_tool("dart_dividend"), {"year": 2025}, SCOPE, text, True, [KEY])
    assert not r.ok and KEY not in r.text and "***" in r.text


def test_secret_in_success_text_is_redacted():
    r = normalize_result(_tool("dart_dividend"), {"year": 2025}, SCOPE, f"echo {KEY}", False, [KEY])
    assert KEY not in r.text


def test_plain_text_result_passes_through():
    r = normalize_result(
        _tool("krx_stock_price"), {"start_date": "2026-11-20", "end_date": "2026-11-20"}, SCOPE,
        "plain text", False, [KEY],
    )
    assert r.ok and r.text == "plain text"


def test_run_converts_arguments_and_calls_server():
    calls = []

    class Caller:
        def call(self, server, tool, arguments):
            calls.append((server, tool, arguments))
            return '{"status":"000","list":[]}', False

    assert _spec(Caller(), "dart_dividend").run({"year": 2025}).ok
    assert calls == [
        ("opendart", "get_dividend_info", {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011"})
    ]


def test_run_wraps_server_failure_and_redacts():
    class Broken:
        def call(self, *a):
            raise ExternalMcpError(f"spawn failed crtfc_key={KEY}")

    r = _spec(Broken(), "dart_dividend").run({"year": 2025})
    assert not r.ok and "외부 도구 사용 불가" in r.text and KEY not in r.text


def test_invalid_arguments_do_not_reach_caller():
    class Caller:
        def call(self, *a):
            raise AssertionError("호출되면 안 된다")

    r = _spec(Caller(), "krx_stock_price").run({"start_date": "2026-01-01", "end_date": "2026-12-31"})
    assert not r.ok and "31일" in r.text


def test_listing_without_caller_fails_softly():
    r = build_external_tools(None, SCOPE, [])[0].run({"year": 2025})
    assert not r.ok and "외부 도구 사용 불가" in r.text


def test_rows_become_a_table_with_constant_fields_hoisted():
    rows = [
        {"rcept_no": "20260310002820", "stlm_dt": "2025-12-31", "corp_code": "00126380",
         "nm": f"주주{i}", "trmend_posesn_stock_qota_rt": f"{i}.00"}
        for i in range(27)
    ]
    raw = json.dumps({"status": "000", "message": "정상", "list": rows}, ensure_ascii=False, indent=2)
    r = normalize_result(_tool("dart_largest_shareholder"), {"year": 2025}, SCOPE, raw, False, [KEY])
    data = json.loads(r.text)
    assert data["rcept_no"] == "20260310002820" and data["stlm_dt"] == "2025-12-31"
    assert data["columns"] == ["nm", "trmend_posesn_stock_qota_rt"]
    assert data["rows"][26] == ["주주26", "26.00"]
    assert r.text.count("nm") == 1


def test_single_row_keeps_all_columns():
    raw = '{"status":"000","list":[{"rcept_no":"1","se":"주당 현금배당금(원)","thstrm":"1,446"}]}'
    data = json.loads(normalize_result(_tool("dart_dividend"), {"year": 2025}, SCOPE, raw, False, [KEY]).text)
    assert data["columns"] == ["rcept_no", "se", "thstrm"] and data["rows"] == [["1", "주당 현금배당금(원)", "1,446"]]
