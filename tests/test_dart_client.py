import httpx
import pytest

from app.config import Settings
from app.dart.client import DartApiClient
from app.dart.dto import DartListResponse
from app.dart.errors import DartApiError

CORP_CODE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>
    <corp_eng_name>SAMSUNG</corp_eng_name><stock_code>005930</stock_code>
    <modify_date>20240101</modify_date></list>
  <list><corp_code>01568413</corp_code><corp_name>F&amp;F</corp_name>
    <corp_eng_name>FnF</corp_eng_name><stock_code>383220</stock_code>
    <modify_date>20240102</modify_date></list>
</result>""".encode("utf-8")


def test_list_response_status_flags():
    ok = DartListResponse.from_json({"status": "000", "list": [{"corp_code": "1", "report_nm": "사업보고서"}]})
    assert ok.has_data and not ok.no_data
    assert ok.items[0].report_nm == "사업보고서"

    empty = DartListResponse.from_json({"status": "013", "message": "조회된 데이터가 없습니다."})
    assert empty.no_data and not empty.has_data


def test_require_zip_raises_dart_error_from_error_xml():
    client = DartApiClient.__new__(DartApiClient)
    body = b"<result><status>020</status><message>\xed\x95\x9c\xeb\x8f\x84\xec\xb4\x88\xea\xb3\xbc</message></result>"
    with pytest.raises(DartApiError) as e:
        client._require_zip(body)
    assert "020" in str(e.value)


def test_parse_corp_code_xml():
    codes = DartApiClient._parse_corp_code_xml(CORP_CODE_XML)
    assert [c.corp_code for c in codes] == ["00126380", "01568413"]
    assert codes[1].corp_name == "F&F"


def _accounts_client(payload):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=payload)

    client = DartApiClient(Settings(dart_api_key="k"))
    client._client = httpx.Client(base_url="http://x/api", transport=httpx.MockTransport(handler))
    return client, captured


def test_fetch_key_accounts_returns_list_and_empty_on_no_data():
    client, captured = _accounts_client(
        {"status": "000", "list": [{"account_nm": "매출액", "thstrm_amount": "1,000"}]}
    )
    rows = client.fetch_key_accounts("00126380", 2024, "11011")
    assert [r["account_nm"] for r in rows] == ["매출액"]
    assert captured["path"].endswith("/fnlttSinglAcnt.json")
    assert captured["params"]["bsns_year"] == "2024"
    assert captured["params"]["reprt_code"] == "11011"
    assert captured["params"]["corp_code"] == "00126380"

    empty, _ = _accounts_client({"status": "013", "message": "조회된 데이타가 없습니다."})
    assert empty.fetch_key_accounts("00126380", 2024, "11011") == []


def test_fetch_key_accounts_raises_dart_error_on_other_status():
    client, _ = _accounts_client({"status": "020", "message": "한도초과"})
    with pytest.raises(DartApiError):
        client.fetch_key_accounts("00126380", 2024, "11011")
