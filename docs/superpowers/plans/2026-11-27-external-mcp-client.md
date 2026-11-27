# 외부 MCP 클라이언트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 에이전트 경로가 외부 MCP 서버 두 개(`opendart-mcp`, `pykrx-mcp`)의 도구 5개를 기존 도구 6개와 함께 쓰게 한다. 설정으로 켜고 끄며, 기본은 꺼짐이다.

**Architecture:** 외부 도구를 기존 `ToolSpec`으로 감싼다. `ToolSpec.run`은 요청마다 띄우는 MCP 세션 풀(`ExternalMcpPool`)로 `tools/call`을 보내고, 결과를 정리해 `ToolResult`로 돌려준다. Function Calling 루프와 우리 MCP 서버(Claude 경로)는 같은 `ToolSpec` 목록을 받으므로 세 provider가 같은 도구를 본다.

**Tech Stack:** Python 3.14, `mcp` 2.3 클라이언트(`ClientSession`, `stdio_client`), `anyio` 블로킹 포털, `uvx`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-external-mcp-client-design.md`

## Global Constraints

- 외부 서버 패키지와 버전: `opendart-mcp@0.1.0`, `pykrx-mcp@0.1.3`. 실행 명령은 `uvx --with "mcp<2" <패키지>@<버전>`이다.
- 이 저장소의 `mcp` 의존성(`>=2.3,<3`)은 그대로 둔다. 외부 패키지를 `pyproject.toml`에 넣지 않는다.
- 설정 `agent_external_mcp: bool = False`(기본 꺼짐), `agent_external_mcp_timeout_seconds: int = 30`.
- 모델에게 보이는 외부 도구 이름은 정확히 `dart_largest_shareholder`, `dart_dividend`, `dart_executives`, `dart_financial_index`, `krx_stock_price` 순서다.
- DART 키는 모델 입력, 답변, 도구 결과, 트레이스, 로그, 평가 결과 파일 어디에도 남지 않는다.
- 외부 결과도 `ToolResult.of`의 4,000자 상한을 거친다.
- 고정 RAG 경로(`agent=false`)와 `ToolExecutor`는 바꾸지 않는다.
- Claude는 구독 CLI로만 쓴다. Anthropic API 키를 쓰지 않는다.
- 코드 주석, 독스트링, 사용자에게 보이는 문구는 기존처럼 한국어로 쓴다.
- 작업은 `feat/external-mcp` 브랜치(워크트리 `.claude/worktrees/external-mcp`)에서 한다.

## Review Focus

1. DART 서버가 HTTP 오류를 내면 예외 문구에 `crtfc_key=<키>`가 담긴 채 `is_error=true`로 온다. 결과 텍스트에서 키가 `***`로 바뀌어야 한다. (Task 1 테스트)
2. 실행 환경에 `uv`가 없거나 명령이 잘못돼 서버가 안 뜨면, 그 호출만 `ok=False`("외부 도구 사용 불가")로 끝나고 에이전트는 계속 답해야 한다. (Task 2, Task 3 테스트)
3. 외부 서버가 응답 없이 멈추면 `agent_external_mcp_timeout_seconds` 안에 실패로 끝나야 한다. 같은 요청 안의 다음 호출은 기다리지 않고 바로 실패해야 한다. (Task 2 테스트)
4. 외부 서버가 JSON이 아닌 일반 텍스트를 돌려주면 정리 단계가 죽지 않고 텍스트를 그대로 넘겨야 한다. (Task 1 테스트)
5. 모델이 31일 넘는 기간이나 `YYYYMMDD` 형식 날짜를 넘기면, 서버를 부르기 전에 고칠 방법이 적힌 오류가 나야 한다. (Task 1 테스트)

---

### Task 0: 작업 공간

- [ ] **Step 1:** `main`에서 `feat/external-mcp` 브랜치와 워크트리 `.claude/worktrees/external-mcp`를 만들고, 저장소 루트의 `.env`를 워크트리에 복사한다(권한 600).
- [ ] **Step 2:** 워크트리에서 `uv run pytest -q`를 실행한다. 기대: 285 passed.

---

### Task 1: 외부 도구 등록 표와 결과 정리

순수 함수만 있는 모듈이다. 프로세스와 네트워크를 쓰지 않는다.

**Files:**
- Create: `app/agent/external_mcp.py`
- Test: `tests/test_external_mcp.py`

**Interfaces:**
- Consumes: `app.agent.tools.ToolSpec`, `ToolResult`, `REPORT_CODES`, `TOOL_NAMES`. `app.ingest.notify.redact`(`crtfc_key=...`를 가린다).
- Produces:
  - `ToolScope(corp_code: str, corp_name: str, stock_code: str)` (frozen dataclass)
  - `ExternalServer(name: str, package: str, secret_env: dict[str, str])`. `secret_env`는 자식 환경변수 이름에서 `Settings` 속성 이름으로 가는 표다. 예: `{"OPENDART_API_KEY": "dart_api_key"}`. 메서드 `command() -> list[str]`는 `["uvx", "--with", "mcp<2", package]`를 돌려준다.
  - `ExternalTool(name, server, remote_name, description, input_schema, to_remote, sources_of)`. `to_remote: Callable[[dict, ToolScope], dict]`는 입력이 잘못되면 한국어 `ValueError`를 낸다. `sources_of: Callable[[object, dict, ToolScope], list[dict]]`.
  - `EXTERNAL_SERVERS: dict[str, ExternalServer]`. 키는 `"opendart"`와 `"pykrx"`다.
  - `EXTERNAL_TOOLS: tuple[ExternalTool, ...]`, `EXTERNAL_TOOL_NAMES: tuple[str, ...]`
  - `class ToolCaller(Protocol)`: `call(server: str, tool: str, arguments: dict) -> tuple[str, bool]`. 반환값은 (결과 텍스트, is_error)다. 실패하면 `ExternalMcpError`를 낸다.
  - `class ExternalMcpError(RuntimeError)`
  - `redact_secrets(text: str, secrets: Iterable[str]) -> str`
  - `normalize_result(tool: ExternalTool, arguments: dict, scope: ToolScope, text: str, is_error: bool, secrets: Sequence[str]) -> ToolResult`
  - `build_external_tools(caller: ToolCaller | None, scope: ToolScope, secrets: Sequence[str]) -> list[ToolSpec]`. `caller`가 `None`이면 목록용이다. 이때 `run`은 `ok=False`("외부 도구 사용 불가")를 돌려준다.

**정해진 값:**

| 이름 | 서버 · 원격 도구 | 모델 인자 (`additionalProperties: false`) | `to_remote` 결과 |
| --- | --- | --- | --- |
| `dart_largest_shareholder` | opendart · `get_largest_shareholder` | `year`(integer, 2015~2100, 필수), `report`(enum `annual`·`half`·`q1`·`q3`, 기본 `annual`) | `{"corp_code", "bsns_year": str(year), "reprt_code": REPORT_CODES[report]}` |
| `dart_dividend` | opendart · `get_dividend_info` | 위와 같음 | 위와 같음 |
| `dart_executives` | opendart · `get_executive_status` | 위와 같음 | 위와 같음 |
| `dart_financial_index` | opendart · `get_single_financial_index` | 위 두 개 + `category`(enum `profitability`·`stability`·`growth`·`activity`, 필수) | 위 + `"idx_cl_code"`: `M210000`·`M220000`·`M230000`·`M240000` |
| `krx_stock_price` | pykrx · `get_stock_ohlcv` | `start_date`, `end_date`(string `YYYY-MM-DD`, 둘 다 필수) | `{"ticker": stock_code, "start_date": "YYYYMMDD", "end_date": "YYYYMMDD"}` |

`krx_stock_price` 날짜 검사:
- 형식이 `YYYY-MM-DD`가 아니면 `ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")`.
- 시작일이 종료일보다 늦으면 `ValueError("시작일이 종료일보다 늦습니다")`.
- `(end - start).days > 30`이면 `ValueError("기간은 최대 31일입니다. 기간을 나눠서 호출하세요")`.

도구 설명(모델에게 보이는 문구):
- `dart_largest_shareholder`: "DART 정기보고서의 최대주주와 특수관계인 보유 주식 수·지분율을 조회한다."
- `dart_dividend`: "DART 정기보고서의 배당 현황(주당 현금배당금, 배당성향, 배당수익률 등)을 조회한다."
- `dart_executives`: "DART 정기보고서의 임원 현황(이름, 직위, 등기 여부, 담당 업무)을 조회한다."
- `dart_financial_index`: "DART 주요 재무지표를 조회한다. category: profitability(수익성: 영업이익률·ROE 등), stability(안정성: 부채비율 등), growth(성장성), activity(활동성)."
- `krx_stock_price`: "기간별 일별 주가(시가·고가·저가·종가)와 거래량을 조회한다. 수정주가 기준이며 기간은 최대 31일이다."

`normalize_result`는 다음 순서로 처리한다.
1. 텍스트를 `redact_secrets`로 가린다. 가리는 대상은 키 문자열 자체와 `crtfc_key=...` 패턴이다.
2. `is_error`이면 `ToolResult(False, "외부 도구 오류: " + 앞 500자)`를 돌려준다.
3. JSON으로 읽어 본다. 실패하면 `ToolResult.of(True, 텍스트)`를 돌려준다.
4. opendart 결과에서 `status`가 `"000"`이 아니면 `ToolResult(False, f"DART {status} {message}")`를 돌려준다. pykrx 결과에서 dict에 `error` 키가 있으면 `ToolResult(False, error 값)`를 돌려준다.
5. 그 밖에는 dict 안 어디에 있든 `corp_code`, `corp_cls`, `corp_name`, `stock_code`, `status`, `message` 키를 지운다. 그다음 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`로 줄여 `ToolResult.of(True, …, sources)`를 돌려준다.

`sources_of`가 만드는 출처:
- **DART:** `list` 행의 `rcept_no`를 중복 없이 순서대로 모아, 행마다 `{"kind": "external", "server": "opendart", "tool": name, "corp_name": scope.corp_name, "rcept_no": …, "label": LABELS[name]}`를 만든다.
- **KRX:** `{"kind": "external", "server": "pykrx", "tool": name, "label": f"KRX 시세 (pykrx) {start_date}~{end_date}"}` 하나를 만든다.
- **LABELS:** `"DART API · 최대주주 현황"`, `"DART API · 배당"`, `"DART API · 임원 현황"`, `"DART API · 재무지표"`.

`build_external_tools`가 만드는 `ToolSpec.run`은 다음 순서로 동작한다.
- `to_remote`에서 `ValueError`가 나면 `ToolResult(False, str(e))`를 돌려준다. 이때 caller는 부르지 않는다.
- 그 밖에는 `caller.call(tool.server, tool.remote_name, 변환된 인자)`를 부르고 결과를 `normalize_result`에 넘긴다.
- `ExternalMcpError`가 나면 `ToolResult(False, "외부 도구 사용 불가: " + 가린 문구)`를 돌려준다.

- [ ] **Step 1: 실패하는 테스트를 쓴다** (`tests/test_external_mcp.py`)

```python
SCOPE = ToolScope("00126380", "삼성전자", "005930")
KEY = "SECRETKEY123"

def test_external_tool_names_are_fixed_and_valid():
    assert EXTERNAL_TOOL_NAMES == ("dart_largest_shareholder", "dart_dividend", "dart_executives",
                                   "dart_financial_index", "krx_stock_price")
    assert not set(EXTERNAL_TOOL_NAMES) & set(TOOL_NAMES)
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in EXTERNAL_TOOL_NAMES)

def test_schemas_hide_company_arguments():
    for t in EXTERNAL_TOOLS:
        assert not {"corp_code", "ticker"} & set(t.input_schema["properties"])

def test_dart_arguments_are_converted_and_corp_injected():
    t = _tool("dart_dividend")
    assert t.to_remote({"year": 2025}, SCOPE) == {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011"}
    assert t.to_remote({"year": 2025, "report": "half"}, SCOPE)["reprt_code"] == "11012"
    assert _tool("dart_financial_index").to_remote({"year": 2025, "category": "stability"}, SCOPE)["idx_cl_code"] == "M220000"

def test_krx_arguments_are_converted_and_ticker_injected():
    assert _tool("krx_stock_price").to_remote({"start_date": "2026-11-20", "end_date": "2026-11-26"}, SCOPE) == \
        {"ticker": "005930", "start_date": "20261120", "end_date": "20261126"}

@pytest.mark.parametrize("start,end,msg", [("2026-01-01", "2026-03-01", "31일"),
                                            ("20261120", "20261126", "YYYY-MM-DD"),
                                            ("2026-11-26", "2026-11-20", "늦습니다")])
def test_krx_rejects_bad_ranges(start, end, msg):
    with pytest.raises(ValueError, match=msg):
        _tool("krx_stock_price").to_remote({"start_date": start, "end_date": end}, SCOPE)

def test_dart_success_is_compacted_with_sources():
    raw = json.dumps({"status": "000", "message": "정상", "list": [
        {"rcept_no": "20260310002820", "corp_code": "00126380", "corp_name": "삼성전자", "nm": "홍라희"},
        {"rcept_no": "20260310002820", "corp_code": "00126380", "nm": "이재용"}]}, ensure_ascii=False, indent=2)
    r = normalize_result(_tool("dart_largest_shareholder"), {"year": 2025}, SCOPE, raw, False, [KEY])
    assert r.ok and "corp_code" not in r.text and "\n" not in r.text and "홍라희" in r.text
    assert r.sources == [{"kind": "external", "server": "opendart", "tool": "dart_largest_shareholder",
                          "corp_name": "삼성전자", "rcept_no": "20260310002820", "label": "DART API · 최대주주 현황"}]

def test_dart_no_data_status_is_failure():
    r = normalize_result(_tool("dart_dividend"), {"year": 2099}, SCOPE,
                         '{"status": "013", "message": "조회된 데이타가 없습니다."}', False, [KEY])
    assert not r.ok and "013" in r.text

def test_pykrx_error_key_is_failure():
    r = normalize_result(_tool("krx_stock_price"), {"start_date": "2026-11-20", "end_date": "2026-11-26"}, SCOPE,
                         '{"error": "No data found", "ticker": "005930"}', False, [KEY])
    assert not r.ok and "No data found" in r.text

def test_error_text_is_redacted():
    text = f"Client error '400' for url 'https://opendart.fss.or.kr/api/alotMatter.json?crtfc_key={KEY}&corp_code=1'"
    r = normalize_result(_tool("dart_dividend"), {"year": 2025}, SCOPE, text, True, [KEY])
    assert not r.ok and KEY not in r.text and "***" in r.text

def test_plain_text_result_passes_through():
    r = normalize_result(_tool("krx_stock_price"), {"start_date": "2026-11-20", "end_date": "2026-11-20"}, SCOPE,
                         "plain text", False, [KEY])
    assert r.ok and r.text == "plain text"

def test_run_converts_calls_and_wraps_errors():
    calls = []
    class Caller:
        def call(self, server, tool, arguments):
            calls.append((server, tool, arguments)); return ('{"status":"000","list":[]}', False)
    spec = {s.name: s for s in build_external_tools(Caller(), SCOPE, [KEY])}["dart_dividend"]
    assert spec.run({"year": 2025}).ok
    assert calls == [("opendart", "get_dividend_info", {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011"})]

    class Broken:
        def call(self, *a): raise ExternalMcpError(f"spawn failed crtfc_key={KEY}")
    r = {s.name: s for s in build_external_tools(Broken(), SCOPE, [KEY])}["dart_dividend"].run({"year": 2025})
    assert not r.ok and "외부 도구 사용 불가" in r.text and KEY not in r.text

def test_invalid_arguments_do_not_reach_caller():
    class Caller:
        def call(self, *a): raise AssertionError("호출되면 안 된다")
    spec = {s.name: s for s in build_external_tools(Caller(), SCOPE, [KEY])}["krx_stock_price"]
    assert not spec.run({"start_date": "2026-01-01", "end_date": "2026-12-31"}).ok

def test_listing_without_caller_fails_softly():
    assert not build_external_tools(None, SCOPE, [])[0].run({"year": 2025}).ok
```

`_tool(name)`은 `EXTERNAL_TOOLS`에서 이름으로 찾는 테스트 헬퍼다.

- [ ] **Step 2:** `uv run pytest tests/test_external_mcp.py -q`를 실행한다. 기대: import 오류로 FAIL.
- [ ] **Step 3:** 위 Interfaces와 정해진 값대로 `app/agent/external_mcp.py`를 구현한다.
- [ ] **Step 4:** `uv run pytest tests/test_external_mcp.py -q`를 실행한다. 기대: 전부 PASS.
- [ ] **Step 5:** 커밋한다. `git commit -m "feat(agent): 외부 MCP 도구 등록 표와 결과 정리"`

---

### Task 2: 요청 단위 MCP 세션 풀

**Files:**
- Create: `app/agent/mcp_pool.py`
- Create: `tests/fixtures/fake_mcp_server.py`
- Test: `tests/test_mcp_pool.py`

**Interfaces:**
- Consumes: `ExternalMcpError`, `redact_secrets`, `EXTERNAL_SERVERS` (Task 1). `app.config.Settings`.
- Produces:
  - `class ExternalMcpPool`
    - `__init__(self, commands: Mapping[str, list[str]], env: Mapping[str, str], timeout_seconds: float, secrets: Sequence[str])`
    - `call(self, server: str, tool: str, arguments: dict) -> tuple[str, bool]`
    - `close(self) -> None`
    - 같은 인스턴스는 `ToolCaller` 프로토콜을 만족한다.
  - `child_env(settings: Settings) -> dict[str, str]`. `PATH`, `HOME`, (있으면) `UV_CACHE_DIR`와 `EXTERNAL_SERVERS`의 `secret_env`에서 나온 값만 담는다.
  - `secrets_of(settings: Settings) -> list[str]`. `dart_api_key`, `openrouter_api_key`, `tavily_api_key` 중 빈 값이 아닌 것을 담는다.
  - `open_external_pool(settings: Settings) -> ExternalMcpPool`

**동작:**
- 포털(`anyio.from_thread.start_blocking_portal`)과 서버 세션은 처음 `call` 때 만든다. 세션은 `portal.wrap_async_context_manager`로 `stdio_client` → `ClientSession`을 열고 `initialize()`까지 한다.
- 서버마다 세션 하나를 요청 동안 다시 쓴다.
- 시작과 호출을 각각 `anyio.fail_after(timeout_seconds)`로 감싼다.
- 시작 실패, 시간 초과, 연결 끊김은 `ExternalMcpError`로 바꾸고 그 서버를 "사용 불가"로 표시한다. 같은 풀에서 그 서버를 다시 부르면 바로 `ExternalMcpError`를 낸다.
- 자식 stderr는 `stdio_client(params, errlog=<임시 파일>)`로 받는다. `close()`에서 끝부분 20줄을 `redact_secrets`로 가려 `logger.info`로 남긴다.
- `close()`는 세션, 포털, 임시 파일을 닫는다. 여러 번 불러도 안전하고, 한 번도 `call`하지 않았어도 안전하다.
- 결과 텍스트는 `TextContent`의 `text`를 이어 붙인 것이다. `is_error`는 MCP 결과의 `is_error`다.

**가짜 서버** `tests/fixtures/fake_mcp_server.py`는 `mcp.server.lowlevel.Server`로 만든다(`app/agent/mcp_server.py`와 같은 2.x API). 시작할 때 stderr에 `"boot crtfc_key=" + os.environ.get("FAKE_SECRET", "")`를 한 줄 쓴다. 도구는 다음과 같다.
- `echo`: `{"pid": os.getpid(), "arguments": arguments, "env": sorted(os.environ)}`를 JSON 텍스트로 돌려준다.
- `sleep`: 10초 기다린다.
- `fail`: `is_error=True`와 텍스트 `"boom"`을 돌려준다.

- [ ] **Step 1: 실패하는 테스트를 쓴다** (`tests/test_mcp_pool.py`)

```python
FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")]

def _pool(timeout=10, env=None):
    base = {"PATH": os.environ["PATH"], "FAKE_SECRET": "S3CRET"}
    return ExternalMcpPool({"fake": FAKE}, env or base, timeout, ["S3CRET"])

def test_call_reuses_one_process():
    pool = _pool()
    try:
        a = json.loads(pool.call("fake", "echo", {"x": 1})[0])
        b = json.loads(pool.call("fake", "echo", {"x": 2})[0])
    finally:
        pool.close()
    assert a["pid"] == b["pid"] and b["arguments"] == {"x": 2}

def test_child_gets_only_given_env(monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")
    pool = _pool()
    try:
        env = json.loads(pool.call("fake", "echo", {})[0])["env"]
    finally:
        pool.close()
    assert "FAKE_SECRET" in env and "SHOULD_NOT_LEAK" not in env

def test_error_result_is_reported():
    pool = _pool()
    try:
        assert pool.call("fake", "fail", {}) == ("boom", True)
    finally:
        pool.close()

def test_timeout_marks_server_unavailable():
    pool = _pool(timeout=1)
    try:
        started = time.monotonic()
        with pytest.raises(ExternalMcpError):
            pool.call("fake", "sleep", {})
        assert time.monotonic() - started < 6
        started = time.monotonic()
        with pytest.raises(ExternalMcpError):
            pool.call("fake", "echo", {})
        assert time.monotonic() - started < 1
    finally:
        pool.close()

def test_start_failure_raises():
    pool = ExternalMcpPool({"bad": ["/nonexistent/server"]}, {"PATH": os.environ["PATH"]}, 5, [])
    try:
        with pytest.raises(ExternalMcpError):
            pool.call("bad", "echo", {})
    finally:
        pool.close()

def test_close_is_safe_without_calls_and_twice():
    pool = _pool()
    pool.close(); pool.close()

def test_stderr_is_logged_redacted(caplog):
    caplog.set_level(logging.INFO)
    pool = _pool()
    pool.call("fake", "echo", {})
    pool.close()
    assert "S3CRET" not in caplog.text and "***" in caplog.text

def test_child_env_has_only_allowed_keys(monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")
    env = child_env(Settings(_env_file=None, dart_api_key="K"))
    assert env["OPENDART_API_KEY"] == "K" and "SHOULD_NOT_LEAK" not in env and "PATH" in env
```

- [ ] **Step 2:** `uv run pytest tests/test_mcp_pool.py -q`를 실행한다. 기대: import 오류로 FAIL.
- [ ] **Step 3:** 가짜 서버와 `app/agent/mcp_pool.py`를 구현한다.
- [ ] **Step 4:** `uv run pytest tests/test_mcp_pool.py -q`를 실행한다. 기대: 전부 PASS, 30초 안.
- [ ] **Step 5:** 커밋한다. `git commit -m "feat(agent): 요청 단위 외부 MCP 세션 풀 (anyio 포털, 시간 초과, stderr 가리기)"`

---

### Task 3: 에이전트 경로에 연결 (local · deepseek), 설정, 프롬프트, 출처

**Files:**
- Modify: `app/config.py` (설정 두 개 추가)
- Modify: `app/agent/service.py` (`run`, `_collect_sources`)
- Modify: `app/agent/prompts.py`
- Modify: `.env.example` (`AGENT_EXTERNAL_MCP=false`)
- Test: `tests/test_agent_service.py`, `tests/test_agent_prompts.py`(없으면 만든다)

**Interfaces:**
- Consumes: `build_external_tools`, `ToolScope`, `EXTERNAL_TOOL_NAMES` (Task 1). `open_external_pool`, `secrets_of` (Task 2).
- Produces:
  - `build_agent_system_prompt(corp_name: str, corp_code: str, today: date, external: bool = False) -> str`
  - `external_scope(corp_code: str) -> ToolScope | None`(`app/agent/external_mcp.py`에 추가). `WATCHLIST`에 없으면 `None`이다.

**동작:**
- `AgentService.run`은 `settings.agent_external_mcp`가 참이고 `external_scope(corp_code)`가 있을 때만 외부 도구를 붙인다. 이때 `open_external_pool(settings)`로 풀을 열고, `build_tools(ctx) + build_external_tools(pool, scope, secrets_of(settings))`를 `ToolExecutor`에 넘긴다. 풀은 `finally`에서 닫는다.
- Claude 분기는 Task 4에서 다룬다. 여기서는 `external` 값을 프롬프트에만 넘긴다.
- `_collect_sources`에 `kind == "external"` 분기를 더한다. 결과는 `SourceItem(corpName=source.get("corp_name"), reportNm=source["label"], rceptNo=source.get("rcept_no"), sectionTitle=None, docType="DART_API" if source["server"] == "opendart" else "KRX")`다.
- 프롬프트는 `external=True`일 때 규칙 2를 아래 문구로 바꾸고, 규칙 6과 7을 끝에 더한다. `external=False`이면 지금 문구와 글자 하나까지 같다.
  - 규칙 2: "2. web_search는 공시 도구로 근거를 찾지 못했거나, 공시에 없는 정보(업계 동향 등)를 물을 때만 사용하세요."
  - 규칙 6: "6. 최대주주·배당·임원·재무지표는 dart_largest_shareholder, dart_dividend, dart_executives, dart_financial_index를 먼저 사용하세요."
  - 규칙 7: "7. 주가와 거래량은 krx_stock_price를 사용하세요. 기간은 최대 31일입니다."

- [ ] **Step 1: 실패하는 테스트를 쓴다.** 기존 `tests/test_agent_service.py`의 가짜 패턴(`open_tool_context`, 루프 monkeypatch)을 따른다.
  - `test_external_tools_absent_when_disabled`: 설정이 꺼져 있으면 executor 도구 이름이 `list(TOOL_NAMES)`이고 `open_external_pool`이 불리지 않는다.
  - `test_external_tools_added_and_pool_closed_when_enabled`: 설정을 켜면 도구 이름이 `list(TOOL_NAMES) + list(EXTERNAL_TOOL_NAMES)`이다. 루프가 예외를 내도 가짜 풀의 `close`가 한 번 불린다.
  - `test_external_tools_skipped_for_unknown_company`: 워치리스트 밖 `corp_code`면 설정을 켜도 6개다.
  - `test_collect_sources_maps_external`: DART와 KRX external 출처가 위 `SourceItem` 값으로 바뀐다.
  - `test_prompt_unchanged_without_external`: `build_agent_system_prompt(..., external=False)` 결과가 `AGENT_SYSTEM_PROMPT.format(...)`과 같다.
  - `test_prompt_with_external_adds_rules`: 결과에 `krx_stock_price`와 `dart_dividend`가 있고 "주가, 업계 동향"은 없다.
- [ ] **Step 2:** `uv run pytest tests/test_agent_service.py tests/test_agent_prompts.py -q`를 실행한다. 기대: 새 테스트 FAIL.
- [ ] **Step 3:** 구현한다.
- [ ] **Step 4:** `uv run pytest -q`를 실행한다. 기대: 전부 PASS.
- [ ] **Step 5:** 커밋한다. `git commit -m "feat(agent): 설정으로 외부 MCP 도구를 에이전트에 붙인다 (기본 꺼짐)"`

---

### Task 4: Claude 경로 (우리 MCP 서버가 외부 도구를 중계)

**Files:**
- Modify: `app/agent/mcp_server.py`
- Modify: `app/agent/claude_agent.py`
- Modify: `app/agent/service.py` (Claude 분기에 `external` 전달)
- Test: `tests/test_agent_mcp_server.py`, `tests/test_agent_claude.py`

**Interfaces:**
- Consumes: Task 1~3의 것.
- Produces:
  - `build_server(corp_code, max_calls, trace_path, settings, external: bool = False) -> Server`
  - `build_mcp_config(corp_code, trace_path, max_tool_calls, external: bool = False) -> dict`. env에 `"AGENT_EXTERNAL_MCP": "1"` 또는 `"0"`이 들어간다.
  - `build_claude_command(question, system_prompt, model, mcp_config_path, tool_names: Sequence[str] = TOOL_NAMES) -> list[str]`
  - `run_claude_agent(question, system_prompt, corp_code, settings, runner=subprocess.run)`는 `settings.agent_external_mcp`를 읽어 위 두 함수에 넘긴다.

**동작:**
- `mcp_server.main()`은 `os.environ.get("AGENT_EXTERNAL_MCP") == "1"`로 `external`을 정한다.
- 목록은 `build_tools(빈 컨텍스트)`에 `external`이고 scope가 있을 때 `build_external_tools(None, scope, [])`를 더한다.
- 호출 핸들러는 첫 호출 때 executor를 만든다. 이때 `external`이면 `open_external_pool(settings)`로 풀을 열어 같은 목록을 넣는다. 풀은 `main()`의 `finally`에서 닫는다.
- `run_claude_agent`의 CLI 실패 예외 문구는 stderr를 `redact_secrets(stderr, secrets_of(settings))`로 가린 것으로 바꾼다.

- [ ] **Step 1: 실패하는 테스트를 쓴다.**
  - `test_mcp_config_external_flag`: `external=True`이면 env의 `AGENT_EXTERNAL_MCP`가 `"1"`이고, 기본이면 `"0"`이다. 기존 비밀값 없음 검사도 그대로 통과한다.
  - `test_claude_command_includes_external_tools_only_when_given`: 기본 명령에는 `mcp__company__krx_stock_price`가 없다. `tool_names=TOOL_NAMES + EXTERNAL_TOOL_NAMES`이면 있다.
  - `test_run_claude_agent_redacts_stderr`: runner가 returncode 1, stderr `"failed crtfc_key=K123"`을 돌려주고 settings의 `dart_api_key="K123"`이면, `RuntimeError` 문구에 `K123`이 없다.
  - `test_mcp_server_lists_external_tools_over_stdio_when_enabled`: 기존 stdio 목록 테스트를 본떠 env에 `AGENT_EXTERNAL_MCP=1`을 넣는다. 목록이 `list(TOOL_NAMES) + list(EXTERNAL_TOOL_NAMES)`이다. 외부 서버는 띄우지 않는다.
  - 기존 `test_mcp_server_lists_tools_over_stdio_without_external_services`는 그대로 6개를 기대한다.
- [ ] **Step 2:** 해당 테스트 파일을 실행한다. 기대: 새 테스트 FAIL.
- [ ] **Step 3:** 구현한다.
- [ ] **Step 4:** `uv run pytest -q`를 실행한다. 기대: 전부 PASS.
- [ ] **Step 5:** 커밋한다. `git commit -m "feat(agent): Claude 경로에서 우리 MCP 서버가 외부 MCP 도구를 중계"`

---

### Task 5: 평가 연결과 새 문항

**Files:**
- Modify: `app/eval/dataset.py` (도구 이름 검사)
- Modify: `app/eval/cli.py` (`run --external-mcp`)
- Modify: `eval/dataset.yaml` (새 문항 12개)
- Create: `tests/test_external_mcp_live.py`
- Test: `tests/test_eval_dataset.py`, `tests/test_eval_runner.py` 또는 CLI 테스트가 있는 파일

**Interfaces:**
- Consumes: `EXTERNAL_TOOL_NAMES`, `open_external_pool`.
- Produces:
  - `KNOWN_TOOL_NAMES = TOOL_NAMES + EXTERNAL_TOOL_NAMES`(`app/eval/dataset.py`)
  - `run --external-mcp` 플래그. 켜면 `settings.model_copy(update={"agent_external_mcp": True})`로 바꾸고, 결과 파일 `config["external_mcp"]`에 `true`를 기록하고, 파일 이름 끝에 `_ext`를 붙인다(예: `…_deepseek_k6_agent_ext.json`). 끄면 `config["external_mcp"]`는 `false`다.

- [ ] **Step 1: 실패하는 테스트를 쓴다.**
  - `test_expected_tools_accepts_external_names`: `expected_tools: [dart_dividend]` 문항이 읽힌다.
  - 알 수 없는 이름은 계속 `DatasetError`다.
  - `test_cli_external_mcp_flag_sets_config_and_name`: CLI 테스트 패턴을 따른다.
- [ ] **Step 2:** 실행한다. 기대: FAIL.
- [ ] **Step 3:** 구현하고 `uv run pytest -q`를 실행한다. 기대: 전부 PASS. 커밋한다. `git commit -m "feat(eval): 외부 MCP 도구 이름 허용, run --external-mcp"`
- [ ] **Step 4: 실제 서버 확인 테스트.** `tests/test_external_mcp_live.py`를 만든다. 환경변수 `RUN_EXTERNAL_MCP=1`이 없으면 건너뛴다. 실제 `open_external_pool(get_settings())`로 다음을 확인한다.
  - 서버별 `tools/list`에 등록 표의 `remote_name`이 있다.
  - 각 도구의 `to_remote` 결과 키가 원격 스키마의 `properties` 안에 있다.
  - `krx_stock_price` 2026-11-20~2026-11-26과 `dart_dividend` 2025가 `ok=True`다.

  `RUN_EXTERNAL_MCP=1 uv run pytest tests/test_external_mcp_live.py -q`를 실행한다. 기대: PASS. `krx_stock_price`가 실패하면 스펙대로 이 도구를 등록 표에서 빼고(이름 상수, 프롬프트 규칙 7, 테스트 함께), 그 사실을 커밋 메시지에 적는다.
- [ ] **Step 5: 새 문항 12개.** 아래 질문의 정답을 실제 외부 도구 호출(Task 2 풀)로 받아 `eval/dataset.yaml`에 추가한다. `origin: manual`, `answerable: true`로 둔다. `expected_answer`는 출처(접수번호 또는 "KRX 시세")를 포함한 한 문장이다. `must_include`는 답에 꼭 있어야 할 숫자 문자열이다. `expected_sections`는 비운다. id는 `x0001`부터 매긴다.

| 기업 (corp_code) | 질문 | expected_tools |
| --- | --- | --- |
| 삼성전자 (00126380), SK하이닉스 (00164779), 현대자동차 (00164742) 각각 | "2025년 사업보고서 기준 최대주주 본인의 보통주 지분율은?" | `[dart_largest_shareholder, search_disclosures]` |
| 같은 세 기업 각각 | "2025년 보통주 주당 현금배당금은 얼마야?" | `[dart_dividend]` |
| 같은 세 기업 각각 | "2025년 사업보고서 기준 영업이익률은?" | `[dart_financial_index, get_financials]` |
| 같은 세 기업 각각 | "2026년 11월 20일 종가는 얼마야?" | `[krx_stock_price]` |

  `uv run python -m app.eval` 정답셋 검증(기존 `load` 경로)과 `uv run pytest -q`가 통과하는지 확인한다. 커밋한다. `git commit -m "eval: 외부 MCP 도구 문항 12개 (최대주주, 배당, 영업이익률, 종가)"`

---

### Task 6: 수동 스모크와 문서

**Files:**
- Modify: `docs/architecture.md` (전체 그림과 에이전트 경로 그림에 외부 MCP 두 개 추가)
- Modify: `README.md` (에이전트 도구 표, `AGENT_EXTERNAL_MCP` 설명)
- Modify: `docs/rag-quality-history.md` (시각, 변경, 확인 결과 한 줄)

- [ ] **Step 1: 스모크.** `.env`에 `AGENT_EXTERNAL_MCP=true`를 임시로 두고 서버를 띄운다. 삼성전자로 아래 두 질문을 `agent=true`로 보낸다. 끝나면 설정을 원래대로 되돌린다.
  - 질문: "2025년 주당 배당금은?", "2026년 11월 20일 종가는?"
  - provider: `local`과 `claude`. `deepseek`는 크레딧이 없으면 건너뛰고 그렇게 기록한다.
  - 확인: 답, `toolCalls`에 외부 도구 이름이 있는지, `sources`의 `docType`이 `DART_API`·`KRX`인지, 서버 로그와 응답에 DART 키가 없는지(`grep`).
- [ ] **Step 2:** `docs/architecture.md`를 고친다. Mermaid가 그려지는지 브라우저로 확인한다(예약어 노드 id 주의: `graph`, `end`).
- [ ] **Step 3:** README와 품질 이력을 고친다.
- [ ] **Step 4:** `uv run pytest -q`를 실행한다. 기대: 전부 PASS. 커밋한다. `git commit -m "docs: 외부 MCP 도구 구조, 설정, 이력"`

---

### Task 7: 켜기 판단 평가 (OpenRouter 크레딧 충전 후)

크레딧이 없으면 이 작업은 대기로 남기고, 다른 작업을 끝낸 상태로 보고한다.

- [ ] **Step 1:** 외부 도구를 끈 상태로 실행한다. `uv run python -m app.eval run --provider deepseek --agent --judge claude`
- [ ] **Step 2:** 외부 도구를 켠 상태로 실행한다. `uv run python -m app.eval run --provider deepseek --agent --judge claude --external-mcp`
- [ ] **Step 3:** `app.eval compare`로 두 결과를 비교한다. 스펙의 기준을 적용한다. 기존 52문항 정답이 2문항 넘게 줄지 않고, 새 12문항 정답이 늘면 `app/config.py`의 `agent_external_mcp` 기본값을 `True`로 바꾼다. 결과를 `docs/rag-quality-history.md`에 기록하고 커밋한다.
