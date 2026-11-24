"""에이전트 도구 6개의 정의(JSON Schema)와 실행 함수. 로컬 모델과 Claude(MCP)가 같은 정의를 쓴다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import httpx
from neo4j import Driver

from app.agent.calculator import calculate
from app.agent.tavily import TavilyClient, TavilyError
from app.dart.client import DartApiClient
from app.dart.errors import DartApiError
from app.services.company_chat import CYPHER_ROUTER_PROMPT_TEMPLATE, WRITE_CLAUSE, CompanyChatService
from app.services.disclosure_graph import DisclosureGraphService
from app.watchlist import WATCHLIST

MAX_RESULT_CHARS = 4000
TRUNCATION_MARK = "…(생략)"
NO_RESULT = "결과 없음"

TOOL_NAMES: tuple[str, ...] = (
    "search_disclosures",
    "get_financials",
    "list_recent_disclosures",
    "query_ownership_graph",
    "calculate",
    "web_search",
)

REPORT_CODES = {"annual": "11011", "half": "11012", "q1": "11013", "q3": "11014"}
DISCLOSURE_TYPES = list("ABCDEFGHIJ")

# 그래프 스키마 설명은 Text2Cypher 라우터 프롬프트와 같은 것을 쓴다(이중 중괄호는 format 이스케이프).
GRAPH_SCHEMA_TEXT = (
    CYPHER_ROUTER_PROMPT_TEMPLATE.split("스키마:\n", 1)[1]
    .split("\n\n현재 대상", 1)[0]
    .replace("{{", "{")
    .replace("}}", "}")
)

_SINCE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

_LIST_DISCLOSURES_CYPHER = """
MATCH (r:Report)-[:FILED_BY]->(:Company {corp_code: $corp_code})
WHERE r.rcept_dt >= $since AND (size($types) = 0 OR r.pblntf_ty IN $types)
RETURN r.rcept_dt AS rcept_dt, r.pblntf_ty AS pblntf_ty, r.report_nm AS report_nm, r.rcept_no AS rcept_no
ORDER BY r.rcept_dt DESC
LIMIT $limit
"""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str
    sources: list[dict] = field(default_factory=list)

    @staticmethod
    def of(ok: bool, text: str, sources: list[dict] | None = None) -> "ToolResult":
        """text가 MAX_RESULT_CHARS를 넘으면 잘라서 만든다."""
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + TRUNCATION_MARK
        return ToolResult(ok, text, sources or [])


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], ToolResult]


@dataclass
class ToolContext:
    corp_code: str
    chat: CompanyChatService
    graph: DisclosureGraphService
    dart: DartApiClient
    tavily: TavilyClient
    driver: Driver


def _fail(text: str) -> ToolResult:
    return ToolResult(False, text)


def _corp_or_error(ctx: ToolContext, args: dict) -> tuple[str | None, ToolResult | None]:
    corp_code = args.get("corp_code") or ctx.corp_code
    if corp_code not in {c.corp_code for c in WATCHLIST}:
        return None, _fail(f"워치리스트에 없는 기업 코드: {corp_code}")
    return corp_code, None


def _chunk_source(doc) -> dict:
    meta = doc.metadata
    return {
        "kind": "chunk",
        "corp_name": meta.get("corp_name"),
        "report_nm": meta.get("report_nm"),
        "rcept_no": meta.get("rcept_no"),
        "source_file": meta.get("source_file"),
        "section_order": meta.get("section_order"),
        "section_title": meta.get("section_title"),
        "doc_type": meta.get("doc_type"),
        "score": doc.score,
        "text": doc.text,
    }


def _search_disclosures(ctx: ToolContext, args: dict) -> ToolResult:
    corp_code, error = _corp_or_error(ctx, args)
    if error:
        return error
    docs = ctx.chat.retrieve(
        args["query"], corp_code, args.get("top_k", 6), doc_type=args.get("doc_type")
    )
    if not docs:
        return ToolResult.of(True, NO_RESULT)
    text = "\n\n".join(
        f"[{i}] ({d.metadata.get('corp_name')} {d.metadata.get('report_nm')} > "
        f"{d.metadata.get('section_title')}) {d.text}"
        for i, d in enumerate(docs, start=1)
    )
    return ToolResult.of(True, text, [_chunk_source(d) for d in docs])


def _get_financials(ctx: ToolContext, args: dict) -> ToolResult:
    corp_code, error = _corp_or_error(ctx, args)
    if error:
        return error
    try:
        rows = ctx.dart.fetch_key_accounts(corp_code, args["year"], REPORT_CODES[args["report"]])
    except DartApiError as e:
        return _fail(f"DART 조회 실패 (상태 {e.status})")
    except (httpx.HTTPError, ValueError):
        return _fail("DART 조회 실패 (네트워크 오류 또는 응답 형식 오류)")
    if not rows:
        return ToolResult.of(True, NO_RESULT)
    consolidated = [r for r in rows if r.get("fs_div") == "CFS"]
    lines = [
        f"{r.get('account_nm')}: {r.get('thstrm_amount')} ({r.get('frmtrm_amount')})"
        for r in (consolidated or rows)
    ]
    scope = "연결" if consolidated else "별도"
    header = f"{args['year']}년 {args['report']} {scope} 주요계정 (당기 (전기), 단위 원)"
    return ToolResult.of(True, "\n".join([header, *lines]))


def _list_recent_disclosures(ctx: ToolContext, args: dict) -> ToolResult:
    corp_code, error = _corp_or_error(ctx, args)
    if error:
        return error
    since = args.get("since", "")
    if since:
        if not _SINCE_PATTERN.fullmatch(since):
            return _fail("since는 YYYY-MM-DD 형식이어야 합니다")
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            return _fail("since는 실제 존재하는 날짜여야 합니다")
    with ctx.driver.session(default_access_mode="READ") as session:
        records = list(
            session.run(
                _LIST_DISCLOSURES_CYPHER,
                corp_code=corp_code,
                since=since.replace("-", ""),
                types=args.get("types", []),
                limit=args.get("limit", 20),
            )
        )
    if not records:
        return ToolResult.of(True, NO_RESULT)
    lines = []
    for r in records:
        d = r["rcept_dt"]
        lines.append(f"{d[:4]}-{d[4:6]}-{d[6:8]} {r['pblntf_ty']} {r['report_nm']} ({r['rcept_no']})")
    return ToolResult.of(True, "\n".join(lines))


GRAPH_MAX_ROWS = 200
GRAPH_TIMEOUT_SECONDS = 10


def _query_ownership_graph(ctx: ToolContext, args: dict) -> ToolResult:
    cypher = args["cypher"].strip()
    if WRITE_CLAUSE.search(cypher):
        return _fail("읽기 전용 MATCH 쿼리만 허용됩니다")
    try:
        result = ctx.graph.run_read_only_query(
            cypher, max_rows=GRAPH_MAX_ROWS, timeout=GRAPH_TIMEOUT_SECONDS
        )
    except Exception as e:  # noqa: BLE001 - 잘못된 Cypher는 모델이 고칠 수 있게 오류 첫 줄까지 돌려준다
        first_line = (str(e).strip().splitlines() or [""])[0][:200]
        return _fail(f"그래프 쿼리 실패: {type(e).__name__}: {first_line}")
    return ToolResult.of(True, result if result is not None else NO_RESULT)


def _calculate(args: dict) -> ToolResult:
    try:
        value = calculate(args["expression"])
    except ValueError as e:
        return _fail(f"계산 오류: {e}")
    return ToolResult.of(True, format(round(value, 6), "f").rstrip("0").rstrip("."))


def _web_search(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        results = ctx.tavily.search(args["query"], args.get("max_results", 5))
    except TavilyError as e:
        return _fail(str(e))
    if not results:
        return ToolResult.of(True, NO_RESULT)
    text = "\n\n".join(f"[{i}] {r.title}\n{r.url}\n{r.snippet}" for i, r in enumerate(results, start=1))
    return ToolResult.of(
        True, text, [{"kind": "web", "title": r.title, "url": r.url} for r in results]
    )


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_CORP_CODE_PROP = {
    "type": "string",
    "description": "대상 기업 corp_code. 생략하면 현재 대화 중인 기업",
}


def build_tools(ctx: ToolContext) -> list[ToolSpec]:
    return [
        ToolSpec(
            "search_disclosures",
            "공시 원문(사업·반기·분기보고서, 지분공시)과 뉴스 청크를 벡터 검색한다. "
            "사업 내용, 위험 요인, 임원·지분 현황 같은 서술형 근거에 쓴다. 숫자 재무는 get_financials가 정확하다.",
            _schema(
                {
                    "query": {"type": "string", "description": "검색 질의"},
                    "corp_code": _CORP_CODE_PROP,
                    "doc_type": {
                        "type": "string",
                        "enum": ["NEWS"],
                        "description": "NEWS이면 뉴스 기사만 검색. 생략하면 전체",
                    },
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
                },
                ["query"],
            ),
            lambda args: _search_disclosures(ctx, args),
        ),
        ToolSpec(
            "get_financials",
            "DART 주요계정(매출액, 영업이익, 당기순이익, 자산·부채·자본 등)을 조회한다. "
            "연결 기준이 있으면 연결만 돌려주며 단위는 원이다. 재무 수치 질문은 이 도구를 우선 쓴다.",
            _schema(
                {
                    "corp_code": _CORP_CODE_PROP,
                    "year": {"type": "integer", "description": "사업연도(예: 2024)"},
                    "report": {
                        "type": "string",
                        "enum": ["annual", "half", "q1", "q3"],
                        "description": "annual=사업보고서, half=반기, q1=1분기, q3=3분기",
                    },
                },
                ["year", "report"],
            ),
            lambda args: _get_financials(ctx, args),
        ),
        ToolSpec(
            "list_recent_disclosures",
            "최근 공시 목록을 접수일 내림차순으로 조회한다. since 이후, 공시 유형(A~J) 필터를 줄 수 있다.",
            _schema(
                {
                    "corp_code": _CORP_CODE_PROP,
                    "since": {"type": "string", "description": "이 날짜 이후 접수분만 (YYYY-MM-DD)"},
                    "types": {
                        "type": "array",
                        "items": {"type": "string", "enum": DISCLOSURE_TYPES},
                        "description": "공시 유형 코드 목록 (A~J)",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                }
            ),
            lambda args: _list_recent_disclosures(ctx, args),
        ),
        ToolSpec(
            "query_ownership_graph",
            "Neo4j 그래프에서 지분공시 제출인 관계를 조회하는 읽기 전용 Cypher를 실행한다. "
            "MATCH, WHERE, RETURN, ORDER BY, LIMIT, count()만 허용하며 쓰기 구문은 거부된다. "
            "집계·랭킹·교집합·역방향 조회에 쓴다. 결과는 최대 200행, 실행 시간은 10초로 제한된다.\n\n"
            f"스키마:\n{GRAPH_SCHEMA_TEXT}\n\n현재 대상 기업의 corp_code: '{ctx.corp_code}'",
            _schema({"cypher": {"type": "string", "description": "읽기 전용 Cypher 쿼리"}}, ["cypher"]),
            lambda args: _query_ownership_graph(ctx, args),
        ),
        ToolSpec(
            "calculate",
            "사칙연산(+ - * /)과 괄호만 계산한다. 증감률, 비율 같은 산술은 직접 하지 말고 이 도구를 쓴다.",
            _schema({"expression": {"type": "string", "description": "예: (195-171.5)/171.5*100"}}, ["expression"]),
            _calculate,
        ),
        ToolSpec(
            "web_search",
            "웹검색(Tavily). 공시·뉴스 인덱스에 없는 최신 정보나 외부 사실이 필요할 때만 쓴다.",
            _schema(
                {
                    "query": {"type": "string", "description": "검색 질의"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                ["query"],
            ),
            lambda args: _web_search(ctx, args),
        ),
    ]
