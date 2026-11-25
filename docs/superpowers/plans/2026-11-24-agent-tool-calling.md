# 에이전트 도구 호출 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 도구 6개를 한 번만 구현해 로컬(Ollama Function Calling)과 Claude CLI(MCP)에 연결하는 에이전트 채팅 경로를 옵션으로 추가하고, 평가 CLI로 기존 경로와 비교할 수 있게 한다.

**Architecture:** `app/agent/`에 도구 정의(스키마 + 함수), 도구 실행기(한도·반복 차단·JSON Lines 기록), 모델과 무관한 도구 루프, MCP 서버, Claude CLI 실행기, 에이전트 서비스를 둔다. 루프를 우리가 도는 모델은 `StepModel` 인터페이스(1차 구현: Ollama, 나중에 Claude API)를, 루프를 모델이 도는 연결은 Claude CLI + MCP를 쓴다. 두 경로 모두 같은 실행기와 기록 형식을 거쳐 응답 출처와 평가 지표를 만든다.

**Tech Stack:** Python 3.11+, FastAPI, httpx, neo4j, 공식 `mcp` 파이썬 SDK(저수준 `Server` + stdio), Tavily Search API, DART Open API, pytest(+pytest-asyncio)

**Spec:** `docs/superpowers/specs/2026-11-24-agent-tool-calling-design.md`

## Global Constraints

- `ANTHROPIC_API_KEY`를 쓰거나 요구하지 않는다. Claude는 `claude` CLI(구독 로그인)로만 부른다(`CLAUDE.md`).
- Tavily 키는 `.env`의 `TAVILY_API_KEY`에만 있다. 저장소·로그·결과 파일·임시 MCP 설정 파일 어디에도 키를 쓰지 않는다. `.env.example`에는 `TAVILY_API_KEY=` 빈 값만.
- `agent`를 켜지 않은 채팅 요청은 지금과 응답 JSON까지 완전히 같아야 한다(키 집합 `{"answer", "sources"}`).
- 도구 이름(정확히): `search_disclosures`, `get_financials`, `list_recent_disclosures`, `query_ownership_graph`, `calculate`, `web_search`.
- 질문당 도구 실행 최대 **6회**. 같은 도구를 같은 인자로 다시 부르면 실행하지 않고 오류 결과.
- 에이전트용 Ollama 옵션 `num_ctx` 기본 **16384**, 에이전트용 Claude CLI 타임아웃 기본 **180초**. 둘 다 설정으로 바꿀 수 있다. 고정 경로의 기존 옵션은 바꾸지 않는다.
- 도구는 워치리스트(`app/watchlist.py`) 밖 `corp_code`, 빈 결과, 외부 API 오류에서 예외를 던지지 않고 `ToolResult(ok=False 또는 "결과 없음")`을 돌려준다.
- Claude CLI 호출에는 `--strict-mcp-config`를 쓰고, 우리 MCP 도구 6개만 허용하며 CLI 내장 도구(웹검색 포함)는 막는다.
- 고정 경로의 기존 `OllamaClient.generate()`/`chat()`, `ClaudeCliClient.generate()` 반환형은 바꾸지 않는다.
- 단위 테스트는 Neo4j·Ollama·claude CLI·네트워크 없이 돈다(Task 4의 MCP 서버 기동 테스트는 로컬 프로세스만 띄운다).

## Review Focus

1. **데이터가 아직 없는 기업(8곳)에 묻는 경우**: `search_disclosures`·`list_recent_disclosures`가 오류 없이 "결과 없음"을 돌려줘서 모델이 웹검색이나 거절로 넘어가야 한다. → Task 2 `test_search_disclosures_reports_no_results`
2. **모델이 도구 인자를 빠뜨리거나 잘못된 타입으로 부르는 경우**(3B 모델에서 흔함, 슬라이드 67 Arguments): 실행기가 예외로 죽지 않고 인자 오류 결과를 돌려줘야 한다. → Task 2 `test_executor_returns_error_for_bad_arguments`
3. **MCP 서버가 CLI의 작업 디렉터리(임시 폴더)에서 떠서 `.env`를 못 찾는 경우**: DART·Tavily 키가 비어 도구가 전부 실패한다. 서버는 저장소 루트의 `.env`를 명시적으로 읽어야 한다. → Task 4 `test_mcp_server_settings_read_repo_env`
4. **Claude CLI가 실패하거나 타임아웃인데 트레이스 파일은 일부 기록된 경우**: 서비스는 오류를 올리되 임시 파일(설정·트레이스)은 지워야 한다. → Task 5 `test_run_claude_agent_cleans_up_temp_files_on_failure`
5. **에이전트 응답에 웹 출처와 공시 출처가 섞인 경우**: 웹 출처가 공시처럼 보이면 안 된다(`docType="WEB"`). → Task 6 `test_agent_sources_mark_web_results`

---

### Task 1: 외부 데이터 클라이언트 (Tavily, DART 주요계정)와 설정

**Files:**
- Create: `app/agent/__init__.py` (빈 파일), `app/agent/tavily.py`
- Modify: `app/dart/client.py` (메서드 추가), `app/config.py`, `.env.example`, `pyproject.toml` (의존성 `mcp`)
- Test: `tests/test_agent_tavily.py`, `tests/test_dart_client.py`(추가)

**Interfaces:**
- Produces:
  - `Settings`에 `tavily_api_key: str = ""`, `tavily_base_url: str = "https://api.tavily.com"`, `agent_ollama_num_ctx: int = 16384`, `agent_claude_timeout_seconds: int = 180`, `agent_max_tool_calls: int = 6`
  - `@dataclass(frozen=True) class WebResult: title: str; url: str; snippet: str` (`app/agent/tavily.py`)
  - `class TavilyError(RuntimeError)`
  - `TavilyClient(settings)`: `search(query: str, max_results: int = 5) -> list[WebResult]`, `close()`. `POST /search`, 헤더 `Authorization: Bearer <key>`, 본문 `{"query", "max_results", "search_depth": "basic"}`. 키가 비면 `TavilyError("TAVILY_API_KEY가 설정되어 있지 않습니다")`. HTTP 오류는 상태 코드만 담은 `TavilyError`로 바꾼다(응답 본문·헤더·키를 메시지에 넣지 않는다).
  - `DartApiClient.fetch_key_accounts(corp_code: str, year: int, reprt_code: str) -> list[dict]`: `fnlttSinglAcnt.json`(단일회사 주요계정). `reprt_code`는 `"11011"`(사업)·`"11012"`(반기)·`"11013"`(1분기)·`"11014"`(3분기). 응답 `list`를 그대로 돌려주고, 데이터 없음(`status 013`)이면 빈 목록. 다른 오류는 기존 `DartApiError`.
- `pyproject.toml`: `"mcp>=1.2"`를 dependencies에 추가하고 `.venv/bin/pip install -e '.[dev]'`. 설치된 버전의 저수준 서버 API(`mcp.server.lowlevel.Server`, `mcp.server.stdio.stdio_server`, `mcp.types.Tool`)가 import되는지 확인한다. 경로가 다르면 설치된 버전에 맞추고 보고서에 적는다.

- [ ] **Step 1: Write the failing tests** — `httpx.MockTransport`로 요청을 잡는다.

```python
def test_tavily_search_parses_results_and_sends_bearer():
    # 응답 {"results": [{"title": "t", "url": "https://x", "content": "요약"}]}
    assert client.search("삼성전자 HBM", 3) == [WebResult("t", "https://x", "요약")]
    assert captured_headers["authorization"] == "Bearer tvly-test"
    assert captured_json == {"query": "삼성전자 HBM", "max_results": 3, "search_depth": "basic"}

def test_tavily_error_message_never_contains_key():
    # 401 응답 → TavilyError, str(err)에 "tvly-test"가 없음

def test_tavily_requires_key():
    # tavily_api_key="" → TavilyError(match="TAVILY_API_KEY")

def test_fetch_key_accounts_returns_list_and_empty_on_no_data():
    # {"status": "000", "list": [{"account_nm": "매출액", ...}]} → 1개
    # {"status": "013", "message": "조회된 데이타가 없습니다."} → []
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_agent_tavily.py tests/test_dart_client.py -q` → FAIL
- [ ] **Step 3: Implement** — 위 Interfaces대로. `.env.example`에 `# Tavily 웹검색 API 키\nTAVILY_API_KEY=` 추가.
- [ ] **Step 4: Run** 같은 명령 → PASS, 그리고 `.venv/bin/pytest -q` 전체 PASS
- [ ] **Step 5: Commit** `feat(agent): Tavily 검색 클라이언트, DART 주요계정 조회, 에이전트 설정`

---

### Task 2: 도구 정의, 계산기, 도구 실행기와 JSON Lines 기록

**Files:**
- Create: `app/agent/calculator.py`, `app/agent/tools.py`, `app/agent/executor.py`, `app/agent/trace.py`
- Modify: `app/services/company_chat.py` (`retrieve`에 `doc_type` 키워드 인자)
- Test: `tests/test_agent_calculator.py`, `tests/test_agent_tools.py`, `tests/test_agent_executor.py`, `tests/test_company_chat_split.py`(추가)

**Interfaces:**
- Consumes: `TavilyClient`, `WebResult`, `DartApiClient.fetch_key_accounts` (Task 1); `CompanyChatService.retrieve(question, corp_code, top_k)`; `DisclosureGraphService.run_read_only_query(cypher) -> str | None`; `WRITE_CLAUSE`(`app/services/company_chat.py`); `WATCHLIST`
- Produces:
  - `calculate(expression: str) -> float` (`calculator.py`): `ast` 파싱, 숫자 상수·`+ - * /`·단항 `-`·괄호만 허용. 그 외(이름, 호출, `**`, 속성 등)는 `ValueError`. 0으로 나누기는 `ValueError`.
  - `@dataclass(frozen=True) class ToolResult: ok: bool; text: str; sources: list[dict] = field(default_factory=list)` (`tools.py`). `text`는 모델에게 줄 문자열로 최대 `MAX_RESULT_CHARS = 4000`자(넘으면 자르고 "…(생략)"). `sources`는 출처 메타데이터 dict 목록:
    - 청크: `{"kind": "chunk", "corp_name", "report_nm", "rcept_no", "source_file", "section_order", "section_title", "doc_type", "score", "text"}`
    - 웹: `{"kind": "web", "title", "url"}`
  - `@dataclass(frozen=True) class ToolSpec: name: str; description: str; input_schema: dict; run: Callable[[dict], ToolResult]`
  - `TOOL_NAMES: tuple[str, ...]` = Global Constraints의 6개 이름(이 순서)
  - `@dataclass class ToolContext: corp_code: str; chat: CompanyChatService; graph: DisclosureGraphService; dart: DartApiClient; tavily: TavilyClient; driver: Driver`
  - `build_tools(ctx: ToolContext) -> list[ToolSpec]`: 6개, `TOOL_NAMES` 순서. 스키마(JSON Schema, `additionalProperties: false`):
    - `search_disclosures`: `query`(string, 필수), `corp_code`(string), `doc_type`(enum `["NEWS"]`), `top_k`(integer 1~10, 기본 6)
    - `get_financials`: `corp_code`(string), `year`(integer, 필수), `report`(enum `["annual","half","q1","q3"]`, 필수) → `11011/11012/11013/11014`
    - `list_recent_disclosures`: `corp_code`(string), `since`(string `YYYY-MM-DD`), `types`(array of enum `A`~`J`), `limit`(integer 1~50, 기본 20)
    - `query_ownership_graph`: `cypher`(string, 필수). 설명에 그래프 스키마(지금 `CYPHER_ROUTER_PROMPT_TEMPLATE`의 스키마 부분)와 `ctx.corp_code`를 넣는다. 읽기 전용 MATCH만 허용한다는 문구 포함.
    - `calculate`: `expression`(string, 필수)
    - `web_search`: `query`(string, 필수), `max_results`(integer 1~10, 기본 5)
    - `corp_code`가 빠지면 `ctx.corp_code`. 워치리스트 밖이면 `ToolResult(ok=False, text="워치리스트에 없는 기업 코드: …")`.
  - `list_recent_disclosures`의 조회: `(r:Report)-[:FILED_BY]->(:Company {corp_code})`, `r.rcept_dt >= since(YYYYMMDD)`, 유형 필터, `ORDER BY r.rcept_dt DESC LIMIT`. 반환 줄 형식 `YYYY-MM-DD 유형 보고서명 (접수번호)`.
  - `get_financials` 결과: 연결(`fs_div == "CFS"`) 행이 있으면 연결만, 없으면 별도. 줄 형식 `계정명: 당기 금액 (전기 금액)`, 단위 원.
  - `@dataclass(frozen=True) class ToolCallRecord: seq: int; name: str; arguments: dict; ok: bool; result_text: str; sources: list[dict]` (`trace.py`)
  - `append_record(path: str | Path, record: ToolCallRecord) -> None`(한 줄 JSON, `ensure_ascii=False`), `read_records(path: str | Path) -> list[ToolCallRecord]`(파일 없으면 `[]`)
  - `ToolExecutor(specs: list[ToolSpec], max_calls: int, trace_path: str | Path | None = None)` (`executor.py`):
    - `execute(name: str, arguments: dict) -> ToolResult`
    - 모르는 도구 → `ok=False` "알 수 없는 도구". 인자 검증 실패(필수 누락, 타입·enum 불일치, 모르는 키) → `ok=False` "인자 오류: …". 검증은 스키마의 `required`·`type`·`enum`·`minimum/maximum`·`additionalProperties`만 직접 검사한다(새 의존성 없이).
    - 같은 이름 + 같은 인자(키 정렬 JSON 비교) 재호출 → 실행하지 않고 `ok=False` "같은 호출을 반복했습니다. 다른 도구나 인자를 쓰거나 지금까지의 근거로 답하세요".
    - 실행 횟수가 `max_calls`에 이르면 → 실행하지 않고 `ok=False` "도구 호출 한도(6회)를 넘었습니다. 지금까지의 근거로 답하세요"(숫자는 `max_calls`).
    - 도구 함수의 예외 → `ok=False` "도구 오류: <예외 클래스명>"(메시지에 키가 섞일 수 있어 예외 메시지는 넣지 않는다).
    - 모든 호출(거절된 것 포함)을 `ToolCallRecord`로 `records` 속성에 쌓고, `trace_path`가 있으면 `append_record`도 한다. 실행 횟수에는 실제로 도구 함수가 돈 호출만 센다.

- [ ] **Step 1: Write the failing tests**

```python
def test_calculate_basic_and_rejects_unsafe():
    assert calculate("(195.00 - 171.50) / 171.50 * 100") == pytest.approx(13.7026, rel=1e-4)
    for bad in ["__import__('os')", "2**10", "abs(-1)", "1/0"]:
        with pytest.raises(ValueError): calculate(bad)

def test_build_tools_names_and_schemas():
    specs = build_tools(fake_ctx())
    assert tuple(s.name for s in specs) == TOOL_NAMES
    assert all(s.input_schema["additionalProperties"] is False for s in specs)

def test_search_disclosures_returns_chunk_sources():
    # fake chat.retrieve → Document 2개 → ok=True, sources 2개, kind=="chunk", section_order 보존

def test_search_disclosures_reports_no_results():
    # retrieve → [] → ok=True, "결과 없음" in text, sources == []

def test_tool_rejects_corp_outside_watchlist():
    # search_disclosures(query="x", corp_code="99999999") → ok=False

def test_query_ownership_graph_blocks_write_and_empty():
    # "MATCH (n) DETACH DELETE n" → ok=False; run_read_only_query → None → ok=True "결과 없음"

def test_web_search_sources_and_tavily_error():
    # fake tavily 결과 → sources kind=="web"; TavilyError → ok=False

def test_executor_returns_error_for_bad_arguments():
    # execute("calculate", {}) → ok=False "인자 오류"; execute("calculate", {"expression": 1}) → ok=False

def test_executor_blocks_repeat_and_enforces_limit(tmp_path):
    ex = ToolExecutor(build_tools(ctx), max_calls=2, trace_path=tmp_path / "t.jsonl")
    ex.execute("calculate", {"expression": "1+1"}); r = ex.execute("calculate", {"expression": "1+1"})
    assert not r.ok and "반복" in r.text
    ex.execute("calculate", {"expression": "2+2"}); r = ex.execute("calculate", {"expression": "3+3"})
    assert not r.ok and "한도" in r.text
    assert [rec.seq for rec in read_records(tmp_path / "t.jsonl")] == [1, 2, 3, 4]

def test_executor_hides_exception_message():
    # 도구 함수가 RuntimeError("secret tvly-xxx") → text에 "tvly" 없음, "RuntimeError" 있음
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_agent_calculator.py tests/test_agent_tools.py tests/test_agent_executor.py -q` → FAIL
- [ ] **Step 3: Implement** — 위 Interfaces대로. `search_disclosures`는 `ctx.chat.retrieve(query, corp_code, top_k, doc_type=doc_type)`를 쓴다. 이를 위해 `CompanyChatService.retrieve`에 키워드 인자 `doc_type: str | None = None`을 추가한다: 값이 있으면 뉴스 키워드 판정과 상관없이 그 값으로 메타데이터 필터를 걸고 후보군을 `NEWS_FILTER_CANDIDATE_POOL`로 넓힌다. `None`이면 지금 동작 그대로(기존 테스트가 그대로 통과해야 한다). 이 변경과 테스트(`test_retrieve_doc_type_forces_filter`)는 `tests/test_company_chat_split.py`에 넣는다.
- [ ] **Step 4: Run** 같은 명령 → PASS, 전체 `.venv/bin/pytest -q` PASS
- [ ] **Step 5: Commit** `feat(agent): 도구 6개, 계산기, 도구 실행기(한도·반복 차단)와 호출 기록`

---

### Task 3: 모델과 무관한 도구 루프와 Ollama 어댑터

**Files:**
- Create: `app/agent/loop.py`
- Modify: `app/llm/ollama.py` (메서드 추가)
- Test: `tests/test_agent_loop.py`, `tests/test_ollama_client.py`(추가)

**Interfaces:**
- Consumes: `ToolSpec`, `ToolExecutor` (Task 2)
- Produces:
  - `@dataclass(frozen=True) class ToolCall: name: str; arguments: dict` (`loop.py`)
  - `@dataclass(frozen=True) class ModelStep: content: str; tool_calls: list[ToolCall]; prompt_tokens: int | None = None`
  - `class StepModel(Protocol): def step(self, messages: list[dict], tools: list[ToolSpec]) -> ModelStep: ...` — 루프를 우리가 도는 모델의 인터페이스. 나중의 Claude API 구현체도 이것을 구현한다.
  - `@dataclass(frozen=True) class LoopResult: answer: str; prompt_tokens: int | None`
  - `run_tool_loop(model: StepModel, executor: ToolExecutor, system_prompt: str, question: str) -> LoopResult`:
    1. `messages = [system, user]`로 `model.step`.
    2. `tool_calls`가 있으면 각각 `executor.execute`하고, 메시지 목록에 모델 턴(`{"role": "assistant", "content", "tool_calls": [...]}`)과 결과 턴(`{"role": "tool", "content": result.text, "tool_name": name}`)을 붙여 다시 `step`.
    3. `tool_calls`가 없으면 그 `content`가 답.
    4. 안전장치: `step` 호출이 `executor`의 `max_calls + 2`번을 넘으면, 마지막으로 "도구 없이 지금까지의 근거로 답하라"는 user 메시지를 붙이고 `tools=[]`로 한 번 더 불러 그 `content`를 답으로 쓴다.
    - `prompt_tokens`는 각 step 값 중 최대.
  - `OllamaClient.chat_step(messages: list[dict], tools: list[dict]) -> ModelStep`는 만들지 않는다. 대신 `OllamaClient.chat_messages(messages: list[dict], tools: list[dict] | None = None) -> dict`(응답 JSON 그대로, `options`는 기존 규칙대로 붙음)를 추가하고, 변환은 `loop.py`의 `OllamaStepModel(client: OllamaClient)`이 맡는다: `ToolSpec` → `{"type": "function", "function": {"name", "description", "parameters": input_schema}}`, 응답 `message.tool_calls[].function.{name, arguments}` → `ToolCall`(arguments가 문자열이면 JSON 파싱, 실패하면 `{}`).

- [ ] **Step 1: Write the failing tests** — 정해진 `ModelStep`을 차례로 돌려주는 가짜 `StepModel`과 Task 2의 실행기(가짜 도구 컨텍스트)로.

```python
def test_loop_executes_tool_then_returns_answer():
    model = Scripted([ModelStep("", [ToolCall("calculate", {"expression": "1+2"})]), ModelStep("답: 3", [])])
    result = run_tool_loop(model, executor, "sys", "1+2는?")
    assert result.answer == "답: 3"
    assert model.seen[1][-1] == {"role": "tool", "content": "3.0", "tool_name": "calculate"}  # 계산 결과 텍스트 형식은 Task 2 구현을 따른다

def test_loop_forces_final_answer_when_model_never_stops():
    # 매번 다른 인자로 calculate를 부르는 모델, max_calls=2 → 마지막 step의 tools == [] 이고 그 content가 answer

def test_any_step_model_works():  # API 전환 대비: 프로토콜만 구현한 가짜로 루프가 돈다
    ...

def test_ollama_step_model_converts_tools_and_calls():
    # chat_messages가 {"message": {"content": "", "tool_calls": [{"function": {"name": "web_search", "arguments": "{\"query\": \"x\"}"}}]}, "prompt_eval_count": 900}
    # → ModelStep(tool_calls=[ToolCall("web_search", {"query": "x"})], prompt_tokens=900)
    # 보낸 tools[0]["function"]["parameters"] == spec.input_schema

def test_chat_messages_sends_tools_and_options():  # tests/test_ollama_client.py, MockTransport
    # OllamaClient(settings, options={"num_ctx": 16384}).chat_messages(msgs, tools) → 본문에 "tools", "options"
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_agent_loop.py tests/test_ollama_client.py -q` → FAIL
- [ ] **Step 3: Implement** — 위 Interfaces대로
- [ ] **Step 4: Run** → PASS, 전체 PASS
- [ ] **Step 5: Commit** `feat(agent): 모델과 무관한 도구 루프와 Ollama Function Calling 어댑터`

---

### Task 4: MCP 서버 (stdio)

**Files:**
- Create: `app/agent/context.py`, `app/agent/mcp_server.py`
- Test: `tests/test_agent_mcp_server.py`

**Interfaces:**
- Consumes: `build_tools`, `ToolContext`, `ToolExecutor`, `TOOL_NAMES` (Task 2); `Settings` 새 필드 (Task 1)
- Produces:
  - `REPO_ROOT: Path` (`context.py`, 이 파일 기준 상위 두 단계)
  - `repo_settings() -> Settings`: `Settings(_env_file=REPO_ROOT / ".env")`. MCP 서버는 CLI의 임시 작업 폴더에서 뜨므로 반드시 이걸 쓴다.
  - `open_tool_context(settings: Settings, corp_code: str) -> tuple[ToolContext, Callable[[], None]]`: Neo4j 드라이버, 임베딩용 `OllamaClient`, `CompanyVectorStoreRegistry`, `CompanyChatService`(Claude 클라이언트 포함, 도구는 retrieve만 씀), `DisclosureGraphService`, `DartApiClient`, `TavilyClient`를 만들고, 전부 닫는 함수를 함께 돌려준다. 에이전트 서비스(Task 6)도 이 함수를 쓴다.
  - `mcp_tool_list(specs: list[ToolSpec]) -> list[mcp.types.Tool]` (`mcp_server.py`): `name`, `description`, `inputSchema=spec.input_schema`. 스키마를 다시 정의하지 않는다.
  - `python -m app.agent.mcp_server`: 환경변수 `AGENT_CORP_CODE`(필수), `AGENT_TRACE_PATH`(선택), `AGENT_MAX_TOOL_CALLS`(기본 `settings.agent_max_tool_calls`)를 읽는다. 저수준 `Server`에 `list_tools` → `mcp_tool_list`, `call_tool(name, arguments)` → `executor.execute`의 `ToolResult.text`를 `TextContent` 하나로 돌려준다(`ok=False`면 `isError` 표시가 지원되면 켠다). 도구 컨텍스트(Neo4j 연결 등)는 첫 `call_tool`에서 만든다. 그래야 `list_tools`만 하는 경우 외부 서비스 없이 뜬다.

- [ ] **Step 1: Write the failing tests**

```python
def test_mcp_tool_list_matches_specs():
    tools = mcp_tool_list(build_tools(fake_ctx()))
    assert [t.name for t in tools] == list(TOOL_NAMES)
    assert tools[0].inputSchema == build_tools(fake_ctx())[0].input_schema

def test_mcp_server_settings_read_repo_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)              # 임시 폴더에서 떠도
    assert repo_settings().model_config      # REPO_ROOT/.env를 읽도록 _env_file이 절대경로인지 확인
    # (구현: repo_settings()가 REPO_ROOT / ".env"를 넘기는지 monkeypatch로 Settings 생성 인자를 잡아 검사)

async def test_mcp_server_lists_six_tools_over_stdio():
    # mcp 클라이언트(stdio_client + ClientSession)로 `sys.executable -m app.agent.mcp_server`를 띄워
    # env={"AGENT_CORP_CODE": "00126380", "PYTHONPATH": str(REPO_ROOT)}, cwd=임시 폴더
    # list_tools() 이름 == TOOL_NAMES (Neo4j 없이 성공해야 한다)
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_agent_mcp_server.py -q` → FAIL
- [ ] **Step 3: Implement** — 위 Interfaces대로
- [ ] **Step 4: Run** → PASS, 전체 PASS
- [ ] **Step 5: Commit** `feat(agent): 도구 6개를 내보내는 MCP stdio 서버`

---

### Task 5: Claude CLI 에이전트 실행기

**Files:**
- Create: `app/agent/claude_agent.py`
- Test: `tests/test_agent_claude.py`

**Interfaces:**
- Consumes: `REPO_ROOT` (Task 4); `ToolCallRecord`, `read_records` (Task 2); `TOOL_NAMES`
- Produces:
  - `MCP_SERVER_NAME = "company"`
  - `build_mcp_config(corp_code: str, trace_path: Path, max_tool_calls: int) -> dict`: `{"mcpServers": {"company": {"command": sys.executable, "args": ["-m", "app.agent.mcp_server"], "env": {"AGENT_CORP_CODE", "AGENT_TRACE_PATH", "AGENT_MAX_TOOL_CALLS", "PYTHONPATH": str(REPO_ROOT)}}}}`. **비밀값을 넣지 않는다**(서버가 `repo_settings()`로 `.env`를 읽는다).
  - `build_claude_command(question: str, system_prompt: str, model: str, mcp_config_path: Path) -> list[str]`: `claude -p <question> --system-prompt <...> --model <model> --output-format json --mcp-config <path> --strict-mcp-config --allowedTools mcp__company__<name> ...(6개) --disallowedTools Bash Edit Write Read Glob Grep WebSearch WebFetch Task NotebookEdit`
  - `parse_cli_result(stdout: str) -> str`: JSON 출력의 최종 답(`result` 필드). 파싱 실패나 `is_error`가 참이면 `RuntimeError`.
  - `@dataclass(frozen=True) class ClaudeAgentResult: answer: str; records: list[ToolCallRecord]`
  - `run_claude_agent(question: str, system_prompt: str, corp_code: str, settings: Settings, runner: Callable = subprocess.run) -> ClaudeAgentResult`: 임시 폴더에 설정 JSON(권한 600)과 트레이스 경로를 만들고, `cwd=임시 폴더`, `input=""`, `timeout=settings.agent_claude_timeout_seconds`로 실행 → 답 파싱 → `read_records(trace)`. 성공·실패·타임아웃 모두 `finally`에서 임시 폴더를 지운다. 실패 메시지 형식은 `ClaudeCliClient`와 같게(`claude CLI 오류 (exit N): ...`, 타임아웃 문구).

- [ ] **Step 1: Write the failing tests** — `runner`를 가짜로 넘긴다.

```python
def test_mcp_config_has_no_secrets(tmp_path):
    cfg = build_mcp_config("00126380", tmp_path / "t.jsonl", 6)
    dumped = json.dumps(cfg)
    assert "tvly-" not in dumped and "TAVILY" not in dumped and "DART_API_KEY" not in dumped
    assert cfg["mcpServers"]["company"]["env"]["AGENT_CORP_CODE"] == "00126380"

def test_claude_command_allows_only_our_tools():
    cmd = build_claude_command("q", "sys", "sonnet", Path("/tmp/c.json"))
    assert "--strict-mcp-config" in cmd and "WebSearch" in cmd[cmd.index("--disallowedTools"):]
    allowed = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
    assert allowed == [f"mcp__company__{n}" for n in TOOL_NAMES]

def test_run_claude_agent_reads_trace():
    # 가짜 runner가 env의 AGENT_TRACE_PATH(설정 파일에서 읽음)에 기록 2줄을 쓰고 stdout='{"result": "답", "is_error": false}'
    # → answer == "답", len(records) == 2

def test_run_claude_agent_cleans_up_temp_files_on_failure():
    # 가짜 runner가 returncode=1 → RuntimeError, 만든 임시 폴더가 없어졌는지 확인
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_agent_claude.py -q` → FAIL
- [ ] **Step 3: Implement** — 위 Interfaces대로
- [ ] **Step 4: Run** → PASS, 전체 PASS
- [ ] **Step 5: Smoke (Neo4j·Ollama·claude 로그인 필요)** — `.venv/bin/python -c` 한 줄로 `run_claude_agent("삼성전자 2026년 3분기 연결 매출액은?", <임시 시스템 프롬프트>, "00126380", repo_settings())`를 실행해 답과 `records`(최소 1개 도구 호출)를 확인한다. 결과를 보고서에 붙인다.
- [ ] **Step 6: Commit** `feat(agent): MCP 서버를 붙인 Claude CLI 에이전트 실행기`

---

### Task 6: 에이전트 서비스와 채팅 API 연결

**Files:**
- Create: `app/agent/prompts.py`, `app/agent/service.py`
- Modify: `app/models.py`, `app/routers/company_report.py`, `app/dependencies.py`, `app/services/company_chat.py` (`_rewrite_query` → 공개 `rewrite_query`, 기존 호출부 갱신)
- Test: `tests/test_agent_service.py`, `tests/test_company_report_router.py`(추가)

**Interfaces:**
- Consumes: `run_tool_loop`, `OllamaStepModel` (Task 3); `open_tool_context` (Task 4, 설정은 앱의 `get_settings()`를 넘긴다 — FastAPI는 저장소 루트에서 뜨므로 `repo_settings()`가 필요 없다); `run_claude_agent` (Task 5); `ToolExecutor`, `build_tools`, `ToolCallRecord` (Task 2)
- Produces:
  - `AGENT_SYSTEM_PROMPT` (`prompts.py`): 설계 문서 "도구 우선순위" 규칙 1~5를 모두 담는다(문구는 구현자 재량). 현재 기업명과 `corp_code`, 오늘 날짜를 넣는 `build_agent_system_prompt(corp_name: str, corp_code: str, today: date) -> str`.
  - `@dataclass(frozen=True) class AgentResult: answer: str; records: list[ToolCallRecord]; prompt_tokens: int | None`
  - `AgentService(settings: Settings, chat: CompanyChatService, ollama_agent: OllamaClient)` (`service.py`):
    - `run(question: str, corp_code: str, provider: str | None) -> AgentResult`: provider가 `claude`면 `run_claude_agent`, 아니면 `open_tool_context`로 도구를 만들고 `ToolExecutor(max_calls=settings.agent_max_tool_calls)` + `run_tool_loop(OllamaStepModel(ollama_agent), ...)`. 컨텍스트는 끝나면 닫는다.
    - `chat(question: str, corp_code: str, history: list[ChatTurnDto], provider: str | None) -> ChatResponse`: 히스토리가 있으면 `chat.rewrite_query`로 재작성한 질문으로 `run`. 응답 `sources`는 기록의 `sources`를 호출 순서대로 모아 같은 출처는 한 번만: 청크 → `SourceItem(corpName, reportNm, rceptNo, sectionTitle, docType)`, 웹 → `SourceItem(reportNm=title, rceptNo=url, docType="WEB")`. `toolCalls`는 기록마다 `ToolCallTrace`.
  - `app/models.py`: `ChatRequest.agent: bool = False`; `class ToolCallTrace(BaseModel): name: str; arguments: dict; ok: bool; summary: str`(결과 텍스트 앞 200자); `ChatResponse.toolCalls: list[ToolCallTrace] | None = None`
  - 라우터: `request.agent`이면 `container.agent_service.chat(...)`, 아니면 기존 그대로. 엔드포인트에 `response_model_exclude_unset=True`를 준다. 기존 경로는 `toolCalls`를 설정하지 않으므로 응답 JSON이 지금과 같다(기존 `SourceItem` 필드는 생성 시 모두 명시적으로 넣고 있어 그대로 남는다).
  - `Container.agent_service`: `AgentService(settings, chat_service, OllamaClient(settings, options={"num_ctx": settings.agent_ollama_num_ctx}))`. `close()`에서 에이전트용 Ollama 클라이언트도 닫는다.

- [ ] **Step 1: Write the failing tests**

```python
def test_non_agent_response_json_unchanged(client_with_fake_container):
    resp = client.post("/api/company-report/chat", json={"question": "q", "corpCode": "00126380"})
    assert set(resp.json()) == {"answer", "sources"}
    assert set(resp.json()["sources"][0]) == {"corpName", "reportNm", "rceptNo", "sectionTitle", "docType"}

def test_agent_request_routes_to_agent_service(...):
    # {"agent": true} → 가짜 agent_service.chat 호출, 응답에 "toolCalls"

def test_agent_sources_mark_web_results():
    # 기록: search_disclosures(청크 1개) + web_search(웹 1개) → sources[1].docType == "WEB", rceptNo == url

def test_agent_sources_deduplicate_in_call_order():
    # 같은 청크를 두 번 검색 → sources 1개

def test_agent_chat_rewrites_with_history():
    # history 있으면 rewrite_query 결과가 run에 들어감

def test_agent_run_local_uses_tool_loop():
    # open_tool_context와 run_tool_loop를 monkeypatch, provider None → 루프 경로, "claude" → run_claude_agent 경로
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_agent_service.py tests/test_company_report_router.py -q` → FAIL
- [ ] **Step 3: Implement** — 위 Interfaces대로
- [ ] **Step 4: Run** → PASS, 전체 PASS
- [ ] **Step 5: Commit** `feat(agent): 에이전트 서비스와 채팅 API의 agent 옵션`

---

### Task 7: 평가 통합 (`--agent`, 도구 지표, expected_tools)

**Files:**
- Modify: `app/eval/dataset.py`, `app/eval/metrics.py`, `app/eval/runner.py`, `app/eval/cli.py`, `app/eval/compare.py`
- Test: `tests/test_eval_dataset.py`, `tests/test_eval_metrics.py`, `tests/test_eval_runner.py`, `tests/test_eval_compare.py`(추가)

**Interfaces:**
- Consumes: `AgentService.run -> AgentResult`, `ToolCallRecord`, `TOOL_NAMES` (Task 2, 6)
- Produces:
  - `Item.expected_tools: list[str] = []`. `_parse`에서 문자열 목록이 아니거나 `TOOL_NAMES` 밖 이름이 있으면 `DatasetError`(문항 ID 포함). 비어 있으면 YAML에 쓰지 않는다(기존 규칙).
  - `score_item(item, retrieved, answer, context_texts: list[str] | None = None)`: `context_texts`가 있으면 `unsupported_numbers`의 근거로 그것을 쓴다. 없으면 지금처럼.
  - `score_tools(item: Item, records: list[ToolCallRecord]) -> dict[str, int]`: 항상 `tool_calls`(기록 수), `tool_errors`(`ok=False` 수). `item.expected_tools`가 있으면 추가로 `tool_selection`(기대 도구 중 하나라도 `ok` 여부와 상관없이 호출했으면 1), `unnecessary_web`(`web_search`가 기대에 없는데 호출했으면 1).
  - `retrieved_from_records(records: list[ToolCallRecord]) -> list[Retrieved]`: `search_disclosures` 기록의 `kind == "chunk"` 출처를 호출 순서대로 이어 붙인다(Retrieved 필드는 출처 dict에서).
  - `run_agent_item(agent: AgentService, item: Item, provider: str, judge_fn) -> dict`: 기존 `run_item`과 같은 행 형식 + `"tool_calls": [{"seq", "name", "arguments", "ok"}]`. `metrics = score_item(item, retrieved, answer, context_texts=[r.result_text for r in records]) | score_tools(item, records)`. 단계는 `agent`(에이전트 실행)와 `score`. 판정자에게는 `retrieved_from_records` 결과를 넘긴다.
  - CLI `run --agent`: 에이전트 경로로 돌린다. 이때 Ollama 옵션은 `{"temperature", "seed", "num_ctx": settings.agent_ollama_num_ctx}`. `config.agent = True|False`를 결과 파일에 남긴다. 결과 파일 이름에 `_agent`를 붙인다(예: `20261124-153000_local_k6_agent.json`; `--top-k`는 에이전트 경로에서 쓰지 않지만 이름 형식은 유지).
  - `HIGHER_IS_BETTER`에 `"tool_selection": True, "unnecessary_web": False, "tool_errors": False` 추가(`tool_calls`는 넣지 않는다).

- [ ] **Step 1: Write the failing tests**

```python
def test_expected_tools_validated(tmp_path):
    # expected_tools: ["web_search"] → 로드 성공; ["google"] → DatasetError("q0001"); "web_search"(문자열) → DatasetError

def test_score_tools_selection_and_unnecessary_web():
    item = _item(expected_tools=["get_financials"])
    recs = [rec("search_disclosures", ok=True), rec("web_search", ok=False)]
    assert score_tools(item, recs) == {"tool_calls": 2, "tool_errors": 1, "tool_selection": 0, "unnecessary_web": 1}
    assert set(score_tools(_item(), recs)) == {"tool_calls", "tool_errors"}

def test_retrieved_from_records_keeps_call_order():
    # search_disclosures 두 번(청크 A, B) + web_search → [A, B]

def test_score_item_uses_context_texts_for_numbers():
    # 청크에는 없고 get_financials 결과 텍스트에만 있는 숫자 → context_texts를 넘기면 unsupported 0

def test_run_agent_item_row_shape():
    # 가짜 agent.run → row["tool_calls"][0]["name"], row["metrics"]["tool_calls"], error None

def test_compare_marks_tool_metric_direction():
    # unnecessary_web 0.1 → 0.3 이면 "나빠짐", tool_selection 0.5 → 0.8 이면 "좋아짐"
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_eval_dataset.py tests/test_eval_metrics.py tests/test_eval_runner.py tests/test_eval_compare.py -q` → FAIL
- [ ] **Step 3: Implement** — 위 Interfaces대로
- [ ] **Step 4: Run** → PASS, 전체 PASS
- [ ] **Step 5: Commit** `feat(eval): 에이전트 평가(--agent), 도구 지표, expected_tools`

---

### Task 8: 문서와 완료 확인 실행

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** README에 "에이전트 모드" 절을 추가한다(기존 존댓말 문체): 도구 6개 표, `agent: true` 요청 예시, 로컬·Claude 경로 차이, `TAVILY_API_KEY` 설정(값은 쓰지 않는다), 한도 6회와 반복 차단, `python -m app.eval run --agent`, 새 지표 4개, 화면은 아직 에이전트 모드를 보내지 않는다는 점. 답변 평가 절의 지표 표에도 새 지표를 넣는다.
- [ ] **Step 2: 완료 확인 실행 (Neo4j·Ollama 필요)**
  - `.venv/bin/python -m app.eval run --provider local --top-k 6` → 결과 파일 A
  - `.venv/bin/python -m app.eval run --provider local --agent` → 결과 파일 B
  - `.venv/bin/python -m app.eval compare A B` → 출력을 보고서에 붙인다. B의 행에 `tool_calls`가 있고 오류 행 비율을 확인한다. 에이전트가 더 좋을 필요는 없다(설계 완료 기준).
- [ ] **Step 3: Claude 경로 스모크** — 서버를 띄우지 않고 `AgentService.run("삼성전자 최근 자기주식 처분 결정 내용 알려줘", "00126380", "claude")`를 실행해 답, 도구 기록, 웹 출처 표시를 확인한다(실제 CLI 2~3회 호출).
- [ ] **Step 4:** `git grep -n "tvly-"`가 아무것도 찾지 않는지 확인한다.
- [ ] **Step 5: Commit** `docs(agent): 에이전트 모드 사용법과 평가 지표`
