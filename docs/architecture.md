# 프로젝트 구조

이 문서는 기업 공시 챗봇이 어떤 부품으로 이루어져 있고, 부품끼리 무엇을 주고받는지 그림으로 보여 줍니다. 화살표는 호출 방향입니다. 점선은 매 요청이 아니라 배치나 수동 실행으로 일어나는 흐름입니다. 2026-11-27 기준입니다.

## 전체 그림

FastAPI 서버 하나가 화면, REST API, 답변 생성, 수집을 모두 맡습니다. 데이터는 전부 Neo4j 한 곳에 있습니다. 벡터 검색용 청크와 회사·공시·뉴스 그래프가 같은 DB에 들어 있습니다.

```mermaid
flowchart LR
    user(["사용자 브라우저<br/>static/index.html · admin.html"])

    subgraph server["FastAPI 서버 (app/main.py)"]
        routers["라우터<br/>app/routers/*"]
        chat["고정 RAG 경로<br/>CompanyChatService"]
        agent["에이전트 경로<br/>AgentService"]
        tools["도구 6개<br/>app/agent/tools.py"]
        ingest["수집·색인<br/>IngestRunner · NewsIngestService<br/>AdminDocumentService"]
    end

    subgraph local["이 컴퓨터에서 도는 것"]
        neo4j[("Neo4j (Docker)<br/>벡터 인덱스 + 그래프")]
        ollama["Ollama<br/>qwen2.5:3b 답변<br/>bge-m3 임베딩"]
        cli["claude CLI<br/>(구독 로그인)"]
        mcp["MCP 서버 (stdio)<br/>app/agent/mcp_server.py"]
        extmcp["외부 MCP 서버 (uvx, 기본 꺼짐)<br/>opendart-mcp · pykrx-mcp"]
    end

    subgraph external["외부 서비스 (인터넷)"]
        dart["OpenDART API"]
        naver["네이버 뉴스<br/>(검색·기사 페이지 스크래핑)"]
        tavily["Tavily 웹 검색 API"]
        openrouter["DeepSeek API<br/>deepseek-flash (V4.1 Flash)"]
        krx["KRX · 네이버 금융<br/>(pykrx)"]
        anthropic["Claude (Anthropic)<br/>구독 계정"]
    end

    user -->|"HTTP /api/..."| routers
    routers -->|"agent=false"| chat
    routers -->|"agent=true"| agent
    routers -.->|"색인·뉴스·PDF 업로드"| ingest

    chat -->|"벡터 검색 · Cypher"| neo4j
    chat -->|"질문 임베딩 · local 답변"| ollama
    chat -->|"deepseek 답변"| openrouter
    chat -->|"claude 답변"| cli

    agent -->|"Function Calling<br/>(local)"| ollama
    agent -->|"Function Calling<br/>(deepseek)"| openrouter
    agent -->|"claude -p --mcp-config"| cli
    agent -->|"직접 호출"| tools
    cli -->|"MCP (stdio)"| mcp
    mcp -->|"직접 호출"| tools
    cli --> anthropic

    tools --> neo4j
    tools --> ollama
    tools --> dart
    tools --> tavily
    tools -->|"MCP stdio<br/>(외부 도구 5개)"| extmcp
    extmcp --> dart
    extmcp --> krx

    ingest -.-> dart
    ingest -.-> naver
    ingest -.->|"임베딩"| ollama
    ingest -.->|"청크·노드 저장"| neo4j
```

## 답변이 만들어지는 두 경로

채팅 요청의 `agent` 값이 경로를 고릅니다. `provider` 값(`local`, `deepseek`, `claude`)은 어느 모델이 답할지를 고릅니다.

### 고정 RAG 경로 (`agent=false`)

순서가 코드에 정해져 있습니다. 모델은 검색 결과를 받아 답만 씁니다.

```mermaid
sequenceDiagram
    autonumber
    participant B as 브라우저
    participant R as /api/company-report/chat
    participant S as CompanyChatService
    participant O as Ollama (bge-m3)
    participant N as Neo4j
    participant L as 답변 모델<br/>(Ollama · DeepSeek API · claude CLI)

    B->>R: question, corpCode, provider, agent=false
    R->>S: chat()
    opt 대화 기록이 있을 때
        S->>L: 질문 다시 쓰기 (rewrite_query)
    end
    S->>O: 질문 임베딩
    S->>N: 벡터 검색 (회사별 인덱스, top-k)
    S->>L: Text2Cypher (그래프 질문 생성)
    S->>N: Cypher 실행 (읽기 전용)
    alt Cypher가 없거나 실패하거나 결과가 비었을 때
        S->>N: 검색된 청크의 공시에서 그래프 확장 (VectorCypher)
    end
    S->>L: 시스템 프롬프트(청크 + 그래프) + 질문
    L-->>S: 답변
    S-->>B: answer, sources
```

### 에이전트 경로 (`agent=true`)

모델이 도구를 골라 부르고, 결과를 보고 다시 부르거나 답합니다. 한 질문에 도구 호출은 최대 6번입니다(`agent_max_tool_calls`). 도구 정의는 `app/agent/tools.py` 한 곳에 있고, 연결 방식만 provider마다 다릅니다.

```mermaid
flowchart TB
    q["질문 + corpCode"] --> svc["AgentService"]

    svc -->|"local · deepseek"| loop["우리 서버 안의 루프<br/>app/agent/loop.py"]
    loop <-->|"Function Calling<br/>tools 정의 ↔ tool_calls"| model1["Ollama (qwen2.5:3b)<br/>또는 DeepSeek API"]
    loop -->|"tool_calls 실행"| exec["ToolExecutor<br/>호출 한도 · 기록"]

    svc -->|"claude"| cliproc["claude -p<br/>--strict-mcp-config<br/>--allowedTools mcp__...6개"]
    cliproc <-->|"루프는 CLI 안에서 돈다"| model2["Claude (구독)"]
    cliproc -->|"MCP stdio<br/>tools/list · tools/call"| mcpsrv["mcp_server.py<br/>(요청마다 자식 프로세스)"]
    mcpsrv --> exec2["ToolExecutor<br/>트레이스 JSON Lines 파일"]

    exec --> T
    exec2 --> T

    subgraph T["도구 6개 (tools.py)"]
        t1["search_disclosures<br/>→ Neo4j 벡터 검색 + Ollama 임베딩"]
        t2["list_recent_disclosures<br/>→ Neo4j 그래프"]
        t3["query_ownership_graph<br/>→ Neo4j Cypher (읽기 전용)"]
        t4["get_financials<br/>→ OpenDART API"]
        t5["web_search<br/>→ Tavily API"]
        t6["calculate<br/>→ 로컬 계산"]
    end

    subgraph X["외부 MCP 도구 5개 (external_mcp.py, AGENT_EXTERNAL_MCP=true일 때)"]
        x1["dart_largest_shareholder · dart_dividend<br/>dart_executives · dart_financial_index"]
        x2["krx_stock_price"]
    end
    exec --> X
    exec2 --> X
    X -->|"MCP stdio · 요청마다 띄움<br/>mcp_pool.py"| ext["uvx opendart-mcp 0.1.0<br/>uvx pykrx-mcp 0.1.3"]
    ext --> extapi["OpenDART API · KRX/네이버 금융"]
```

Function Calling과 MCP의 차이는 연결하는 구간입니다.

- **Function Calling은 모델과 앱을 잇습니다.** 모델이 "이 도구를 이 인자로 불러 달라"고 답하고, 앱이 실행합니다. `local`과 `deepseek`는 우리 서버가 앱 역할을 하므로 도구 함수를 직접 부릅니다.
- **MCP는 앱과 도구 서버를 잇습니다.** `claude` 경로에서는 앱 역할을 `claude` CLI가 맡습니다. CLI는 우리 파이썬 함수를 직접 부를 수 없습니다. 그래서 같은 도구를 MCP 서버로 감싸 CLI에 붙입니다. CLI와 Claude 모델 사이는 여전히 Function Calling입니다.
- **외부 MCP 도구에서는 우리 앱이 MCP 클라이언트입니다.** `AGENT_EXTERNAL_MCP=true`이면 `mcp_pool.py`가 `opendart-mcp`와 `pykrx-mcp`를 띄우고 `tools/call`을 보냅니다. `local`과 `deepseek`에서는 우리 서버가 직접 부르고, `claude`에서는 우리 MCP 서버가 서버이면서 클라이언트가 되어 외부 서버로 중계합니다.

## 데이터가 들어오는 길

수집은 세 갈래입니다. 모두 텍스트를 청크로 나누고, Ollama `bge-m3`로 임베딩해, 회사별 Neo4j 벡터 인덱스(`company-report-index-{corp_code}`)에 넣습니다.

```mermaid
flowchart LR
    launchd["launchd 주간 실행<br/>scripts/launchd"] -.-> runner["python -m app.ingest<br/>IngestRunner"]
    adminui["관리자 화면"] -.->|"POST /api/company-report/index"| runner
    adminui -.->|"POST /api/admin/news/fetch"| news["NewsIngestService"]
    adminui -.->|"POST /api/admin/documents"| pdf["AdminDocumentService"]

    runner -->|"공시 목록 · document.xml"| dart["OpenDART API"]
    runner --> xml["xml_reader → splitter<br/>섹션 단위 청크"]
    news -->|"검색 결과 · 기사 본문"| naver["네이버 뉴스"]
    pdf --> pdfsplit["PDF 텍스트 → 청크"]

    xml --> embed["Ollama bge-m3 임베딩"]
    news --> embed
    pdfsplit --> embed
    embed --> vec[("Neo4j 벡터 인덱스<br/>회사별 청크 노드")]

    runner -->|"Company · Report · Filer 노드"| gdb[("Neo4j 그래프")]
    news -->|"Article · Media 노드"| gdb
    runner --> ledger["data/company-report-index.json<br/>data/ingest-state.json"]
    runner -.->|"결과 알림"| notify["macOS 알림 센터"]
```

## Neo4j 그래프 모델

Text2Cypher와 `query_ownership_graph` 도구가 이 스키마로 Cypher를 씁니다. 쓰기 구문(`CREATE`, `MERGE`, `DELETE` 등)은 실행 전에 막습니다.

```mermaid
graph LR
    Filer["Filer<br/>name"] -->|DISCLOSED| Report["Report<br/>rcept_no · report_nm<br/>rcept_dt · pblntf_ty"]
    Report -->|FILED_BY| Company["Company<br/>corp_code · name · stock_code"]
    Media["Media<br/>name"] -->|PUBLISHED| Article["Article<br/>url · title · published_date"]
    Article -->|ABOUT| Company
```

## 평가 파이프라인

`python -m app.eval run`은 정답셋으로 두 경로를 돌리고 채점합니다. 채점 모델은 `claude` CLI(기본 Opus)라 API 과금이 없습니다.

```mermaid
flowchart LR
    ds["eval/dataset.yaml<br/>64문항"] --> run["app.eval.runner"]
    run -->|"agent 없음"| chat["CompanyChatService<br/>retrieve → answer"]
    run -->|"--agent"| ag["AgentService"]
    chat --> rule["규칙 채점 (metrics.py)<br/>검색 적중 · 사실 포함<br/>거절 · 근거 없는 숫자"]
    ag --> rule
    rule --> judge["Claude 채점 (judge.py)<br/>정답 여부 · 근거 충실도<br/>claude CLI"]
    judge --> out["eval/runs/*.json<br/>문항별 결과 + 토큰·비용"]
    out --> cmp["app.eval compare<br/>실행 간 비교"]
```

## 폴더와 역할

| 폴더 | 역할 |
| --- | --- |
| `app/routers` | REST API 입구. 채팅, 색인, 뉴스, 그래프, 관리자 문서 |
| `app/services` | 고정 RAG 답변, 색인, 그래프 조회, 뉴스·PDF 수집 |
| `app/agent` | 에이전트 루프, 도구 6개, 외부 MCP 도구 어댑터와 세션 풀, 실행기, MCP 서버, Claude CLI 연결 |
| `app/llm` | 모델 클라이언트. Ollama, `claude` CLI, OpenAI 호환(DeepSeek API), 사용량 기록 |
| `app/vector` | Neo4j 벡터 저장소, 회사별 인덱스 관리, 청크 분할 |
| `app/dart` | OpenDART API 클라이언트와 공시 XML 파서 |
| `app/naver` | 네이버 뉴스 검색·기사 스크래퍼 (API 키 없음) |
| `app/ingest` | 주간 수집 실행기, 상태 파일, 잠금, 알림 |
| `app/eval` | 정답셋, 실행, 규칙 채점, Claude 채점, 비교 |
| `static` | 빌드된 채팅·관리자 화면 |

## 사용하는 서비스

- **외부 API:** OpenDART, DeepSeek API(deepseek-flash), Tavily 웹 검색
- **외부 MCP 서버(기본 꺼짐):** opendart-mcp, pykrx-mcp(KRX·네이버 금융 시세)
- **Claude:** 로그인된 `claude` CLI(구독)
- **스크래핑:** 네이버 뉴스
- **로컬:** Neo4j(Docker), Ollama
