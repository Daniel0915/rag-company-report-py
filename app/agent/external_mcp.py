"""외부 MCP 서버(opendart-mcp, pykrx-mcp)의 도구 중 일부를 에이전트 도구(ToolSpec)로 감싼다.

여기에는 순수 함수만 둔다. 서버 프로세스와 세션은 app/agent/mcp_pool.py가 맡는다.
회사 인자(corp_code, ticker)는 모델에게 보이지 않고 현재 대화 중인 회사로 넣는다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Protocol, Sequence

from app.agent.tools import REPORT_CODES, ToolResult, ToolSpec
from app.ingest.notify import redact
from app.watchlist import find

UNAVAILABLE = "외부 도구 사용 불가"
ERROR_TEXT_CHARS = 500
MAX_RANGE_DAYS = 30  # 시작일 포함 31일
# 모델에게 필요 없는 반복 필드. 상태 필드는 오류 판정 뒤에 지운다.
DROP_FIELDS = frozenset({"corp_code", "corp_cls", "corp_name", "stock_code", "status", "message"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PYKRX_PACKAGE = "pykrx-mcp@0.1.3"
_AUTH_KRX_SERVER = "pykrx_authenticated"
# pykrx prints login messages on import and refresh. Keep stdout for JSON-RPC.
_PYKRX_STDIO = """\
import sys
from contextlib import redirect_stdout
from functools import partial

with redirect_stdout(sys.stderr):
    from pykrx_mcp.server import main
    from pykrx.website.comm import auth

auth.print = partial(print, file=sys.stderr)
main()
"""


class ExternalMcpError(RuntimeError):
    """외부 MCP 서버를 띄우지 못했거나, 시간 안에 답하지 않았거나, 연결이 끊겼을 때."""


class ToolCaller(Protocol):
    def call(self, server: str, tool: str, arguments: dict) -> tuple[str, bool]:
        """(결과 텍스트, is_error)를 돌려준다. 실패하면 ExternalMcpError."""


@dataclass(frozen=True)
class ToolScope:
    corp_code: str
    corp_name: str
    stock_code: str


@dataclass(frozen=True)
class ExternalServer:
    name: str
    package: str
    secret_env: dict[str, str]  # 자식 환경변수 이름 -> Settings 속성 이름

    def command(self) -> list[str]:
        # 두 서버는 MCP SDK 1.x(FastMCP)로 짜여 있어 이 저장소(2.x)와 분리된 환경에서 띄운다.
        if self.package == _PYKRX_PACKAGE:
            return ["uvx", "--with", "mcp<2", "--from", self.package, "python", "-c", _PYKRX_STDIO]
        return ["uvx", "--with", "mcp<2", self.package]


@dataclass(frozen=True)
class ExternalTool:
    name: str
    server: str
    remote_name: str
    description: str
    input_schema: dict
    to_remote: Callable[[dict, ToolScope], dict]
    sources_of: Callable[[object, dict, ToolScope], list[dict]]


EXTERNAL_SERVERS: dict[str, ExternalServer] = {
    "opendart": ExternalServer("opendart", "opendart-mcp@0.1.0", {"OPENDART_API_KEY": "dart_api_key"}),
    "pykrx": ExternalServer("pykrx", _PYKRX_PACKAGE, {}),
    _AUTH_KRX_SERVER: ExternalServer(
        _AUTH_KRX_SERVER, _PYKRX_PACKAGE, {"KRX_ID": "krx_id", "KRX_PW": "krx_pw"}
    ),
}

_DART_LABELS = {
    "dart_largest_shareholder": "DART API · 최대주주 현황",
    "dart_dividend": "DART API · 배당",
    "dart_executives": "DART API · 임원 현황",
    "dart_financial_index": "DART API · 재무지표",
}
_INDEX_CODES = {
    "profitability": "M210000",
    "stability": "M220000",
    "growth": "M230000",
    "activity": "M240000",
}

_DART_PROPERTIES = {
    "year": {"type": "integer", "minimum": 2015, "maximum": 2100, "description": "사업연도"},
    "report": {
        "type": "string",
        "enum": list(REPORT_CODES),
        "default": "annual",
        "description": "annual(사업보고서), half(반기), q1(1분기), q3(3분기)",
    },
}


def _dart_schema(extra: dict | None = None, required: Sequence[str] = ()) -> dict:
    return {
        "type": "object",
        "properties": {**_DART_PROPERTIES, **(extra or {})},
        "required": ["year", *required],
        "additionalProperties": False,
    }


def _dart_remote(args: dict, scope: ToolScope) -> dict:
    return {
        "corp_code": scope.corp_code,
        "bsns_year": str(args["year"]),
        "reprt_code": REPORT_CODES[args.get("report", "annual")],
    }


def _index_remote(args: dict, scope: ToolScope) -> dict:
    return {**_dart_remote(args, scope), "idx_cl_code": _INDEX_CODES[args["category"]]}


def _parse_date(value: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다") from e


def _krx_remote(args: dict, scope: ToolScope) -> dict:
    start, end = _parse_date(args["start_date"]), _parse_date(args["end_date"])
    if start > end:
        raise ValueError("시작일이 종료일보다 늦습니다")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ValueError("기간은 최대 31일입니다. 기간을 나눠서 호출하세요")
    return {
        "ticker": scope.stock_code,
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
    }


def _dart_sources(name: str) -> Callable[[object, dict, ToolScope], list[dict]]:
    def sources(data: object, args: dict, scope: ToolScope) -> list[dict]:
        rows = data.get("list", []) if isinstance(data, dict) else []
        seen: list[str] = []
        for row in rows:
            rcept_no = row.get("rcept_no") if isinstance(row, dict) else None
            if rcept_no and rcept_no not in seen:
                seen.append(rcept_no)
        return [
            {
                "kind": "external",
                "server": "opendart",
                "tool": name,
                "corp_name": scope.corp_name,
                "rcept_no": r,
                "label": _DART_LABELS[name],
            }
            for r in seen
        ]

    return sources


def _krx_sources(name: str, label: str) -> Callable[[object, dict, ToolScope], list[dict]]:
    def sources(data: object, args: dict, scope: ToolScope) -> list[dict]:
        return [
            {
                "kind": "external",
                "server": "pykrx",
                "tool": name,
                "label": f"{label} {args['start_date']}~{args['end_date']}",
            }
        ]

    return sources


def _krx_tool(name: str, server: str, remote_name: str, description: str, label: str) -> ExternalTool:
    return ExternalTool(
        name, server, remote_name, description,
        {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "시작일 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "종료일 YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
            "additionalProperties": False,
        },
        _krx_remote,
        _krx_sources(name, label),
    )


def _dart_tool(name: str, remote_name: str, description: str) -> ExternalTool:
    return ExternalTool(
        name, "opendart", remote_name, description, _dart_schema(), _dart_remote, _dart_sources(name)
    )


EXTERNAL_TOOLS: tuple[ExternalTool, ...] = (
    _dart_tool(
        "dart_largest_shareholder",
        "get_largest_shareholder",
        "DART 정기보고서의 최대주주와 특수관계인 보유 주식 수·지분율을 조회한다.",
    ),
    _dart_tool(
        "dart_dividend",
        "get_dividend_info",
        "DART 정기보고서의 배당 현황(주당 현금배당금, 배당성향, 배당수익률 등)을 조회한다.",
    ),
    _dart_tool(
        "dart_executives",
        "get_executive_status",
        "DART 정기보고서의 임원 현황(이름, 직위, 등기 여부, 담당 업무)을 조회한다.",
    ),
    ExternalTool(
        "dart_financial_index",
        "opendart",
        "get_single_financial_index",
        "DART 주요 재무지표를 조회한다. category: profitability(수익성: 영업이익률·ROE 등), "
        "stability(안정성: 부채비율 등), growth(성장성), activity(활동성).",
        _dart_schema(
            {"category": {"type": "string", "enum": list(_INDEX_CODES), "description": "지표 분류"}},
            ["category"],
        ),
        _index_remote,
        _dart_sources("dart_financial_index"),
    ),
    _krx_tool(
        "krx_stock_price",
        "pykrx",
        "get_stock_ohlcv",
        "기간별 일별 주가(시가·고가·저가·종가)와 거래량을 조회한다. 수정주가 기준이며 기간은 최대 31일이다.",
        "KRX 시세 (pykrx)",
    ),
    _krx_tool(
        "krx_market_cap", _AUTH_KRX_SERVER, "get_market_cap_by_date",
        "KRX 기준 일별 시가총액(원)과 상장주식수를 조회한다. 기간은 최대 31일이다.",
        "KRX 시가총액 (pykrx)",
    ),
    _krx_tool(
        "krx_market_fundamentals", _AUTH_KRX_SERVER, "get_market_fundamental_by_date",
        "KRX 기준 일별 PER·PBR·EPS·BPS·배당수익률을 조회한다. 기간은 최대 31일이다.",
        "KRX 투자지표 (pykrx)",
    ),
)
EXTERNAL_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in EXTERNAL_TOOLS)


def available_external_tools(krx_auth: bool) -> tuple[ExternalTool, ...]:
    return EXTERNAL_TOOLS if krx_auth else tuple(t for t in EXTERNAL_TOOLS if t.server != _AUTH_KRX_SERVER)


def external_scope(corp_code: str) -> ToolScope | None:
    """워치리스트에 있는 기업만 외부 도구를 쓴다(주가 조회에 stock_code가 필요하다)."""
    company = find(corp_code)
    return ToolScope(company.corp_code, company.name, company.stock_code) if company else None


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """키 문자열 자체와 crtfc_key=... 패턴을 ***로 가린다."""
    text = redact(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _drop_fields(value: object) -> object:
    if isinstance(value, dict):
        return {k: _drop_fields(v) for k, v in value.items() if k not in DROP_FIELDS}
    if isinstance(value, list):
        return [_drop_fields(v) for v in value]
    return value


_ROW_KEYS = ("list", "data")  # opendart는 list, pykrx는 data에 행을 담는다


def _tabulate(data: object) -> object:
    """행 목록을 columns/rows 표로 바꾼다. 모든 행에서 값이 같은 칸(접수번호, 결산일 등)은 위로 올린다.
    행마다 키 이름이 반복되지 않아 최대주주처럼 행이 많은 결과도 4,000자 안에 들어간다."""
    if not isinstance(data, dict):
        return data
    key = next((k for k in _ROW_KEYS if isinstance(data.get(k), list)), None)
    rows = data.get(key) if key else None
    if not rows or not all(isinstance(r, dict) for r in rows):
        return data
    columns = list(dict.fromkeys(c for r in rows for c in r))
    constant = (
        {c: rows[0].get(c) for c in columns if all(r.get(c) == rows[0].get(c) for r in rows)}
        if len(rows) > 1
        else {}
    )
    columns = [c for c in columns if c not in constant]
    rest = {k: v for k, v in data.items() if k != key}
    return {**rest, **constant, "columns": columns, "rows": [[r.get(c) for c in columns] for r in rows]}


def _error_of(server: str, data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    if server == "opendart" and "status" in data and data["status"] != "000":
        return f"DART {data['status']} {data.get('message', '')}".strip()
    if server in ("pykrx", _AUTH_KRX_SERVER) and "error" in data:
        return str(data["error"])
    return None


def normalize_result(
    tool: ExternalTool,
    arguments: dict,
    scope: ToolScope,
    text: str,
    is_error: bool,
    secrets: Sequence[str],
) -> ToolResult:
    text = redact_secrets(text, secrets)
    if is_error:
        return ToolResult(False, f"외부 도구 오류: {text[:ERROR_TEXT_CHARS]}")
    try:
        data = json.loads(text)
    except ValueError:
        return ToolResult.of(True, text)
    error = _error_of(tool.server, data)
    if error:
        return ToolResult(False, error)
    compact = json.dumps(_tabulate(_drop_fields(data)), ensure_ascii=False, separators=(",", ":"))
    return ToolResult.of(True, compact, tool.sources_of(data, arguments, scope))


def _make_run(
    tool: ExternalTool, caller: ToolCaller | None, scope: ToolScope, secrets: Sequence[str]
) -> Callable[[dict], ToolResult]:
    def run(args: dict) -> ToolResult:
        try:
            remote_args = tool.to_remote(args, scope)
        except ValueError as e:
            return ToolResult(False, str(e))
        if caller is None:
            return ToolResult(False, UNAVAILABLE)
        try:
            text, is_error = caller.call(tool.server, tool.remote_name, remote_args)
        except ExternalMcpError as e:
            return ToolResult(False, f"{UNAVAILABLE}: {redact_secrets(str(e), secrets)}")
        return normalize_result(tool, args, scope, text, is_error, secrets)

    return run


def build_external_tools(
    caller: ToolCaller | None, scope: ToolScope, secrets: Sequence[str], *, krx_auth: bool = False
) -> list[ToolSpec]:
    """caller가 None이면 목록용이다(실행하면 '외부 도구 사용 불가')."""
    return [
        ToolSpec(t.name, t.description, t.input_schema, _make_run(t, caller, scope, secrets))
        for t in available_external_tools(krx_auth)
    ]
