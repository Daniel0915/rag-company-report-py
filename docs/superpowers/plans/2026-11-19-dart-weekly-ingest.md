# DART 전 유형 수집 + 주간 자동 수집 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 워치리스트 22곳의 DART 공시를 A~J 전 유형으로 수집하고, 이미 받은 공시는 다운로드 전에 건너뛰며, 매주 토요일 07:10 launchd로 자동 수집하고 실패 시 macOS 알림을 띄운다.

**Architecture:** 기존 `CompanyReportIndexService`를 "공시 1건 색인" 단위로 정리하고, 기록 파일(ledger)·상태 파일·파일 잠금·실행 루프를 `app/ingest/` 패키지로 분리한다. CLI(`python -m app.ingest`)와 기존 `/api/company-report/index` 엔드포인트가 같은 `IngestRunner`를 쓴다. launchd는 CLI의 `update`만 부른다.

**Tech Stack:** Python 3.11+, FastAPI, neo4j 드라이버, httpx, pytest, macOS launchd·osascript

**Spec:** `docs/superpowers/specs/2026-11-19-dart-weekly-ingest-design.md`

## Global Constraints

- 수집 대상 유형: `A,B,C,D,E,F,G,H,I,J` 전부(기본값). `--types`로 좁힐 수 있다.
- 수집 대상 기업: `app/watchlist.py`의 `WATCHLIST` 22곳(기본값). `--corp`로 좁힐 수 있다.
- `update` 조회 기간: 마지막 성공 실행일 **7일 전**부터 오늘까지. 성공 기록이 없으면 오늘부터 2년 전까지.
- `backfill` 기본 기간: 오늘부터 **2년 전**까지.
- 재시도 상한: 공시 하나당 **5번**. 5번 실패하면 재시도 목록에서 빼고 "포기" 목록으로 옮긴다.
- 이미 받은 공시 판단: 기록 파일(`data/company-report-index.json`, 키 `접수번호/파일명`)에 그 접수번호의 키가 하나라도 있으면 **다운로드하지 않는다.**
- 기록 파일이 비어 있는데 Neo4j에 공시 청크(`metadata.doc_type`이 없는 청크)가 있으면 수집을 **거부**한다.
- DART 하루 호출 한도 초과 상태 코드: `020`. 받으면 즉시 멈추고 마지막 성공일을 갱신하지 않는다.
- 파일 잠금: `data/ingest.lock`. CLI와 API가 같은 잠금을 쓴다. 잠금 실패 시 API는 **409**.
- 상태 파일: `data/ingest-state.json`. 로그: `logs/ingest/YYYY-MM-DD.log`, **30일** 보관.
- 알림: 실패·일부 실패만. 제목 `공시 수집 실패` 또는 `공시 수집 완료`. 전부 성공이면 알림 없음.
- launchd 라벨: `com.daniel0915.rag-company-report.ingest`, 매주 토요일(`Weekday` 6) 07:10, `.venv/bin/python -m app.ingest update`.
- 기존 API 응답 형식(`list[IndexResult]`)과 채팅 동작은 바꾸지 않는다.
- Anthropic API 키를 쓰거나 요구하지 않는다(`CLAUDE.md`).
- 단위 테스트는 DART·Neo4j·Ollama 없이 돈다.

## Review Focus

1. **원문 zip에 XML이 하나도 없는 공시**(첨부만 있는 경우): 처리 완료로 기록돼 다음 실행에서 다시 받지 않아야 한다. → Task 4 `test_zip_without_xml_is_recorded_as_processed`
2. **실행 도중 DART `020`(한도 초과)**: 남은 공시를 건드리지 않고 멈추고, 마지막 성공일은 그대로, 이미 처리한 공시는 기록 파일에 남아야 한다. → Task 6 `test_rate_limit_aborts_without_advancing_success_date`
3. **같은 접수번호가 한 실행에서 두 번 나오는 경우**(재시도 목록과 조회 결과에 동시에 있거나 목록 페이지가 겹칠 때): 한 번만 처리해야 한다. → Task 6 `test_same_rcept_no_processed_once_per_run`
4. **기록 파일이 깨진 JSON**: 지금 코드는 빈 기록으로 취급하는데, 그러면 전부 다시 넣어 중복된다. 오류로 멈춰야 한다. → Task 1 `test_corrupt_ledger_raises`
5. **앞선 실행이 비정상 종료하며 남긴 잠금 파일**: 파일이 남아 있어도 잠겨 있지 않으면 새 실행이 잠금을 잡아야 한다. → Task 2 `test_stale_lock_file_does_not_block`

---

### Task 1: 기록 파일(ledger) 분리

**Files:**
- Create: `app/ingest/__init__.py` (빈 파일), `app/ingest/ledger.py`
- Test: `tests/test_ingest_ledger.py`

**Interfaces:**
- Produces:
  - `class LedgerError(RuntimeError)`
  - `class IndexLedger(path: str | Path)`
    - `reload() -> None`: 파일을 다시 읽는다. 파일이 없으면 빈 기록. JSON이 깨졌거나 형식이 다르면 `LedgerError`.
    - `has_rcept(rcept_no: str) -> bool`: `"{rcept_no}/"`로 시작하는 키가 있으면 True
    - `get(key: str) -> LedgerRecord | None`
    - `put(key: str, hash: str, chunk_ids: list[str]) -> None`
    - `mark_processed_without_files(rcept_no: str) -> None`: 키 `"{rcept_no}/-"`, 해시 `""`, 청크 `[]`
    - `save() -> None`: 기존 형식 `{"키": {"hash": ..., "chunkIds": [...]}}`, `ensure_ascii=False`. 임시 파일에 쓰고 `os.replace`로 바꿔 끼운다.
    - `rcept_nos() -> set[str]`, `is_empty() -> bool`
    - `import_from(source: str | Path) -> int`: 지금 기록이 비어 있지 않으면 `LedgerError`. 원본을 읽어 형식을 검증한 뒤 저장하고 가져온 키 수를 돌려준다.
  - `@dataclass class LedgerRecord: hash: str; chunkIds: list[str]`

- [ ] **Step 1: 실패하는 테스트 작성** (`tmp_path` 사용)

```python
def test_has_rcept_matches_any_file_of_the_disclosure(tmp_path):
    ledger = IndexLedger(tmp_path / "l.json")
    ledger.put("20250311001085/20250311001085_00760.xml", "h", ["c1"])
    assert ledger.has_rcept("20250311001085")
    assert not ledger.has_rcept("2025031100108")   # 접두어만 같은 번호는 아님

def test_save_and_reload_keep_existing_format(tmp_path):
    p = tmp_path / "l.json"
    ledger = IndexLedger(p); ledger.put("1/a.xml", "h", ["c1", "c2"]); ledger.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {"1/a.xml": {"hash": "h", "chunkIds": ["c1", "c2"]}}
    again = IndexLedger(p); assert again.get("1/a.xml").chunkIds == ["c1", "c2"]

def test_corrupt_ledger_raises(tmp_path):
    p = tmp_path / "l.json"; p.write_text("{not json", encoding="utf-8")
    with pytest.raises(LedgerError):
        IndexLedger(p)

def test_mark_processed_without_files(tmp_path):
    ledger = IndexLedger(tmp_path / "l.json"); ledger.mark_processed_without_files("9")
    assert ledger.has_rcept("9") and ledger.get("9/-").chunkIds == []

def test_import_from_refuses_when_not_empty(tmp_path):
    src = tmp_path / "src.json"; src.write_text('{"1/a.xml": {"hash": "h", "chunkIds": []}}', encoding="utf-8")
    ledger = IndexLedger(tmp_path / "l.json")
    assert ledger.import_from(src) == 1
    with pytest.raises(LedgerError):
        ledger.import_from(src)
```

- [ ] **Step 2: 실패 확인** — Run: `.venv/bin/pytest tests/test_ingest_ledger.py -v` → Expected: `ModuleNotFoundError: app.ingest`
- [ ] **Step 3: `app/ingest/ledger.py` 구현** (위 Interfaces 그대로. 생성자에서 `reload()` 호출)
- [ ] **Step 4: 통과 확인** — Run: `.venv/bin/pytest tests/test_ingest_ledger.py -v` → Expected: 5 passed
- [ ] **Step 5: Commit** — `git add app/ingest tests/test_ingest_ledger.py && git commit -m "feat(ingest): 색인 기록 파일을 IndexLedger로 분리"`

### Task 2: 파일 잠금

**Files:**
- Create: `app/ingest/lock.py`
- Test: `tests/test_ingest_lock.py`

**Interfaces:**
- Produces:
  - `class IngestBusyError(RuntimeError)`
  - `ingest_lock(path: str | Path) -> ContextManager[None]`: `fcntl.flock(LOCK_EX | LOCK_NB)`. 이미 잠겨 있으면 `IngestBusyError("이미 수집이 실행 중입니다")`. 부모 폴더가 없으면 만든다. 끝나면 잠금만 풀고 파일은 남긴다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_second_acquire_raises_busy(tmp_path):
    p = tmp_path / "ingest.lock"
    with ingest_lock(p):
        with pytest.raises(IngestBusyError):
            with ingest_lock(p):
                pass

def test_stale_lock_file_does_not_block(tmp_path):
    p = tmp_path / "ingest.lock"; p.write_text("")
    with ingest_lock(p):
        pass

def test_lock_released_after_exception(tmp_path):
    p = tmp_path / "ingest.lock"
    with pytest.raises(ValueError):
        with ingest_lock(p):
            raise ValueError
    with ingest_lock(p):
        pass
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/pytest tests/test_ingest_lock.py -v` → ImportError
- [ ] **Step 3: `app/ingest/lock.py` 구현** (`open(path, "a")` 후 `flock`. `flock`은 열린 파일마다 따로라 같은 프로세스 안에서 두 번 열어도 충돌한다)
- [ ] **Step 4: 통과 확인** — 3 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(ingest): 수집 실행 파일 잠금"`

### Task 3: 상태 파일과 조회 기간

**Files:**
- Create: `app/ingest/state.py`
- Test: `tests/test_ingest_state.py`

**Interfaces:**
- Produces:
  - 상수 `OVERLAP_DAYS = 7`, `MAX_ATTEMPTS = 5`
  - `@dataclass PendingFailure: rcept_no: str; corp_code: str; pblntf_ty: str; report_nm: str; rcept_dt: str; error: str; attempts: int`
  - `@dataclass IngestState: last_run_at: str | None = None; last_success_date: date | None = None; pending: list[PendingFailure] = []; given_up: list[PendingFailure] = []`
  - `load_state(path) -> IngestState` (파일 없으면 기본값), `save_state(path, state) -> None` (날짜는 ISO 문자열)
  - `update_window(state: IngestState, today: date) -> tuple[date, date]`
  - `record_failure(state, item: PendingFailure) -> None`: 같은 접수번호가 `pending`에 있으면 `attempts += 1`, 없으면 `attempts=1`로 추가. `attempts >= MAX_ATTEMPTS`가 되면 `given_up`으로 옮긴다. `error`는 최신 값으로 덮어쓴다.
  - `record_success(state, rcept_no: str) -> None`: `pending`에서 뺀다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_window_overlaps_seven_days_from_last_success():
    s = IngestState(last_success_date=date(2026, 11, 14))
    assert update_window(s, date(2026, 11, 21)) == (date(2026, 11, 7), date(2026, 11, 21))

def test_window_without_history_is_two_years():
    assert update_window(IngestState(), date(2026, 11, 21)) == (date(2024, 11, 21), date(2026, 11, 21))

def test_fifth_failure_moves_to_given_up():
    s = IngestState()
    f = PendingFailure("1", "00126380", "I", "영업(잠정)실적(공정공시)", "20261030", "timeout", 0)
    for _ in range(5):
        record_failure(s, replace(f))
    assert s.pending == [] and s.given_up[0].attempts == 5

def test_success_removes_pending():
    s = IngestState(); record_failure(s, PendingFailure("1", "c", "A", "r", "d", "e", 0))
    record_success(s, "1"); assert s.pending == []

def test_state_round_trip(tmp_path):
    p = tmp_path / "s.json"
    s = IngestState(last_run_at="2026-11-21T07:10:00", last_success_date=date(2026, 11, 21))
    save_state(p, s); assert load_state(p) == s
```

- [ ] **Step 2: 실패 확인** → ImportError
- [ ] **Step 3: `app/ingest/state.py` 구현** (2년 전 계산은 `today.replace(year=today.year - 2)`, 2월 29일이면 28일로 내린다)
- [ ] **Step 4: 통과 확인** — 5 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(ingest): 수집 상태 파일과 조회 기간 계산"`

### Task 4: 색인 서비스를 "공시 1건" 단위로 정리

**Files:**
- Modify: `app/services/company_report_index.py` (전체 재구성)
- Modify: `app/services/disclosure_graph.py` (정정 관계 기록 추가)
- Modify: `app/dependencies.py:80-82` (생성자 인자 변경)
- Test: `tests/test_company_report_index.py`

**Interfaces:**
- Consumes: `IndexLedger` (Task 1)
- Produces:
  - `ALL_DISCLOSURE_TYPES = list("ABCDEFGHIJ")`
  - `correction_base_name(report_nm: str) -> str | None`: `^\[[^\]]+\]\s*` 접두어가 있으면 뗀 이름, 없으면 None
  - `CompanyReportIndexService(dart_client, registry, graph_service, ledger: IndexLedger)`
    - `list_disclosures(corp_code: str, bgn_de: date, end_de: date, pblntf_ty: str) -> list[DisclosureItem]` (지금 `_search_all_pages`를 공개로)
    - `index_disclosure(company: WatchedCompany, item: DisclosureItem, pblntf_ty: str) -> IndexResult`
  - `DisclosureGraphService.record_correction(corp_code: str, rcept_no: str, base_name: str) -> str | None`: 같은 기업의 `Report` 중 `report_nm = base_name`이고 `rcept_no <> rcept_no`, `rcept_dt`가 가장 최근인 것을 찾아 `(정정:Report)-[:CORRECTS]->(원본:Report)`를 `MERGE`하고 원본 접수번호를 돌려준다. 없으면 None.
  - `index_watched_companies`는 **삭제**한다(호출자는 Task 7에서 `IngestRunner`로 옮긴다).

`index_disclosure` 순서:
1. `graph.record_disclosure(...)` (지금과 같음)
2. `ledger.has_rcept(item.rcept_no)`면 다운로드 없이 `IndexResult(status="skipped", chunks=0)`
3. `correction_base_name`이 있으면 `graph.record_correction(...)`, 돌려받은 원본 번호를 청크 메타데이터 `corrects_rcept_no`에 넣는다
4. `dart.fetch_document` → `.xml`로 끝나는 파일만 청킹·저장, 파일마다 `ledger.put(f"{rcept_no}/{file}", md5, chunk_ids)` (청크 0개여도 기록)
5. XML 파일이 하나도 없으면 `ledger.mark_processed_without_files(rcept_no)`
6. `ledger.save()`를 **항상** 호출
7. `status`: 새로 저장한 청크가 있으면 `"new"`, 없으면 `"empty"`

`REQUEST_INTERVAL_SECONDS = 0.3`의 쉬어가기는 다운로드 뒤와 목록 페이지마다 지금처럼 유지한다. `threading.Lock`은 삭제한다(잠금은 Task 2가 맡는다).

- [ ] **Step 1: 실패하는 테스트 작성** — DART 클라이언트, 레지스트리, 그래프는 가짜 객체로 바꾼다. 가짜 벡터 스토어는 `add(chunks)`로 받은 문서를 리스트에 쌓는다.

```python
def test_already_recorded_disclosure_is_not_downloaded(tmp_path): ...
    # ledger에 "R1/R1.xml"이 있으면 fake_dart.fetch_document 호출 0회, status == "skipped"
    # graph.record_disclosure는 호출됨

def test_new_disclosure_is_chunked_and_recorded(tmp_path): ...
    # fetch_document가 {"R2.xml": <섹션 1개짜리 최소 DART XML>} → status == "new", chunks >= 1
    # ledger.has_rcept("R2") 이고 저장된 파일에도 키가 있음, 청크 metadata["pblntf_ty"] == "I"

def test_zip_without_xml_is_recorded_as_processed(tmp_path): ...
    # fetch_document가 {"R3.pdf": b"%PDF"} → status == "empty", ledger.has_rcept("R3")

def test_correction_links_original(tmp_path): ...
    # item.report_nm == "[기재정정]사업보고서 (2024.12)", fake graph.record_correction이 "R0"을 돌려줌
    # → record_correction(corp, rcept, "사업보고서 (2024.12)") 호출, 모든 청크 metadata["corrects_rcept_no"] == "R0"

def test_correction_base_name():
    assert correction_base_name("[기재정정]사업보고서 (2024.12)") == "사업보고서 (2024.12)"
    assert correction_base_name("[첨부정정] 영업(잠정)실적(공정공시)") == "영업(잠정)실적(공정공시)"
    assert correction_base_name("사업보고서 (2024.12)") is None
```

  최소 DART XML은 `tests/test_dart_xml_reader.py`가 쓰는 픽스처 형식을 따른다.
- [ ] **Step 2: 실패 확인** — `.venv/bin/pytest tests/test_company_report_index.py -v` → 생성자 인자·함수 없음으로 실패
- [ ] **Step 3: 서비스, `record_correction`, `dependencies.py` 구현** — `dependencies.py`는 `IndexLedger(settings.company_report_index_file)`를 만들어 넘긴다
- [ ] **Step 4: 통과 확인** — `.venv/bin/pytest -q` → 전체 통과(기존 19개 포함)
- [ ] **Step 5: Commit** — `git commit -m "feat(ingest): 전 유형 수집, 접수번호 기준 건너뛰기, 정정 공시 관계 기록"`

### Task 5: 실행 전 점검과 macOS 알림

**Files:**
- Create: `app/ingest/preflight.py`, `app/ingest/notify.py`
- Modify: `app/config.py` (설정 3개 추가)
- Test: `tests/test_ingest_notify.py`

**Interfaces:**
- Produces:
  - `Settings`에 `ingest_state_file: str = "data/ingest-state.json"`, `ingest_lock_file: str = "data/ingest.lock"`, `ingest_log_dir: str = "logs/ingest"`
  - `run_preflight(driver, ollama: OllamaClient, dart: DartApiClient, settings: Settings) -> list[str]`: 문제 설명 목록(빈 목록이면 통과). Neo4j `driver.verify_connectivity()`, Ollama `embed_one("ping")`의 길이가 `settings.embedding_dimension`인지, DART 키가 비어 있지 않은지와 삼성전자(`00126380`) 최근 7일 목록 조회 상태가 `000` 또는 `013`인지. 각 검사는 예외를 잡아 한 줄 설명으로 바꾼다.
  - `send_notification(title: str, message: str) -> None`: `osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' -e 'end run' <title> <message>`. 인자로 넘겨 따옴표 문제를 피한다. 실패해도 예외를 밖으로 던지지 않고 로그만 남긴다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_send_notification_passes_text_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    send_notification('공시 수집 실패', 'Neo4j "연결" 실패')
    assert calls[0][0] == "osascript" and calls[0][-2:] == ['공시 수집 실패', 'Neo4j "연결" 실패']

def test_send_notification_swallows_errors(monkeypatch):
    def boom(*a, **k): raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", boom)
    send_notification("t", "m")   # 예외 없음
```

- [ ] **Step 2: 실패 확인** → ImportError
- [ ] **Step 3: `notify.py`, `preflight.py`, 설정 구현**
- [ ] **Step 4: 통과 확인** — 2 passed
- [ ] **Step 5: Commit** — `git commit -m "feat(ingest): 실행 전 점검과 macOS 알림"`

### Task 6: 실행 루프(IngestRunner)

**Files:**
- Create: `app/ingest/runner.py`
- Modify: `app/ingest/notify.py` (`build_notification` 추가)
- Test: `tests/test_ingest_runner.py`, `tests/test_ingest_notify.py` (추가)

**Interfaces:**
- Consumes: Task 1~5 전부
- Produces:
  - `@dataclass RunSummary: new: int = 0; empty: int = 0; skipped: int = 0; failed: int = 0; given_up: int = 0; chunks: int = 0; aborted_reason: str | None = None; results: list[IndexResult] = []`
  - `IngestRunner(index_service: CompanyReportIndexService, ledger: IndexLedger, settings: Settings)`
    - `run(bgn_de: date, end_de: date, corp_codes: list[str] | None = None, types: list[str] | None = None, today: date | None = None, advance_success_date: bool = True) -> RunSummary`
    - `run_update(today: date | None = None) -> RunSummary`: `update_window`로 기간을 정해 `run(...)`
  - `build_notification(summary: RunSummary) -> tuple[str, str] | None`:
    - `aborted_reason`이 있으면 `("공시 수집 실패", aborted_reason)`
    - `failed > 0` 또는 `given_up > 0`이면 `("공시 수집 완료", f"신규 {new}건, 실패 {failed}건")`, 포기가 있으면 `f", 재시도 중단 {given_up}건"`을 덧붙인다
    - 그 외 None

`run` 순서:
1. `ingest_lock(settings.ingest_lock_file)` 획득(`IngestBusyError`는 그대로 위로)
2. `ledger.reload()`, `load_state(...)`, `state.last_run_at = 지금`
3. 기록 파일이 비어 있으면 `aborted_reason`으로 멈춘다는 규칙은 **여기서 하지 않는다**(Neo4j 조회가 필요해 CLI가 맡는다, Task 8)
4. 재시도 목록(`state.pending`, 대상 기업·유형 필터 적용)을 먼저 처리. `PendingFailure`에서 `DisclosureItem(corp_code, report_nm, rcept_no, rcept_dt)`을 만든다
5. 기업 × 유형마다 `list_disclosures` → `index_disclosure`. 이번 실행에서 처리한 접수번호 집합으로 중복을 거른다
6. 공시 하나의 예외: `DartApiError`이고 `status == "020"`이면 `aborted_reason = "DART 하루 호출 한도 초과"`로 즉시 중단. 그 외 예외는 `record_failure`, `failed += 1`(포기로 옮겨지면 `given_up += 1`). 성공하면 `record_success`
7. `list_disclosures` 자체의 `020`도 같은 중단. 그 외 목록 조회 예외는 그 기업·유형만 건너뛰고 `failed += 1`, 로그에 남긴다
8. 중단되지 않았고 `advance_success_date`이면 `state.last_success_date = today`
9. `save_state`. 모든 공시 결과와 요약은 `logging.getLogger("app.ingest")`로 남긴다

- [ ] **Step 1: 실패하는 테스트 작성** — 가짜 `index_service`(`list_disclosures`는 사전에 정한 목록을, `index_disclosure`는 사전에 정한 결과나 예외를 돌려줌)와 `tmp_path`의 설정(`Settings(ingest_state_file=..., ingest_lock_file=..., company_report_index_file=...)`) 사용

```python
def test_failure_goes_to_pending_and_is_retried_next_run(tmp_path): ...
    # 1회차: R1 예외 → summary.failed == 1, state.pending[0].rcept_no == "R1"
    # 2회차: 목록에 R1이 없어도 index_disclosure(R1)이 호출되고 성공 → pending 비어 있음

def test_rate_limit_aborts_without_advancing_success_date(tmp_path): ...
    # R1 성공, R2에서 DartApiError("020", ...) → R3 미호출, aborted_reason 설정,
    # state.last_success_date 이전 값 그대로, R1은 결과에 있음

def test_same_rcept_no_processed_once_per_run(tmp_path): ...
    # pending에 R1, 목록 조회 결과에도 R1(유형 A와 I 양쪽) → index_disclosure(R1) 1회

def test_success_advances_last_success_date(tmp_path): ...
    # run(..., today=date(2026,11,21)) → load_state(...).last_success_date == date(2026,11,21)

def test_corp_and_type_filters(tmp_path): ...
    # corp_codes=["00126380"], types=["I"] → list_disclosures가 (00126380, "I")로만 호출
```

```python
def test_build_notification():
    assert build_notification(RunSummary(new=3)) is None
    assert build_notification(RunSummary(new=3, failed=2)) == ("공시 수집 완료", "신규 3건, 실패 2건")
    assert build_notification(RunSummary(failed=1, given_up=1)) == ("공시 수집 완료", "신규 0건, 실패 1건, 재시도 중단 1건")
    assert build_notification(RunSummary(aborted_reason="DART 하루 호출 한도 초과")) == ("공시 수집 실패", "DART 하루 호출 한도 초과")
```

- [ ] **Step 2: 실패 확인** → ImportError
- [ ] **Step 3: `runner.py`, `build_notification` 구현**
- [ ] **Step 4: 통과 확인** — `.venv/bin/pytest tests/test_ingest_runner.py tests/test_ingest_notify.py -v` → 전체 통과
- [ ] **Step 5: Commit** — `git commit -m "feat(ingest): 재시도·중복 제거·한도 초과 처리를 갖춘 수집 실행 루프"`

### Task 7: API 엔드포인트를 IngestRunner로 옮기기

**Files:**
- Modify: `app/dependencies.py` (`Container`에 `ingest_runner: IngestRunner` 추가)
- Modify: `app/routers/company_report.py:22-31`
- Test: `tests/test_company_report_router.py`

**Interfaces:**
- Consumes: `IngestRunner.run`, `IngestRunner.run_update`, `IngestBusyError`
- Produces: `POST /api/company-report/index`
  - 기간 없음 → `run_update()`, 기간 있음 → `run(begin, end, advance_success_date=False)`
  - 응답 `summary.results`(`list[IndexResult]`)
  - `IngestBusyError` → 409, `summary.aborted_reason`이 있으면 503과 그 사유

- [ ] **Step 1: 실패하는 테스트 작성** — `FastAPI()`에 `company_report.router`만 붙이고 `app.dependency_overrides[get_container]`로 가짜 컨테이너를 넣는다(실제 `app.main`은 lifespan에서 Neo4j에 연결하므로 쓰지 않는다)

```python
def test_index_without_dates_runs_update(): ...     # run_update 1회, 200, 결과 목록 그대로
def test_index_with_dates_runs_range(): ...         # run(date(2026,1,1), date(2026,1,31), advance_success_date=False)
def test_index_returns_409_when_busy(): ...         # run_update가 IngestBusyError → 409
def test_index_returns_503_when_aborted(): ...      # aborted_reason="DART 하루 호출 한도 초과" → 503
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 라우터와 `Container` 수정**
- [ ] **Step 4: 통과 확인** — `.venv/bin/pytest -q` → 전체 통과
- [ ] **Step 5: Commit** — `git commit -m "feat(ingest): 색인 API를 IngestRunner로 연결, 실행 중이면 409"`

### Task 8: CLI(`python -m app.ingest`)

**Files:**
- Create: `app/ingest/__main__.py`, `app/ingest/cli.py`
- Modify: `.gitignore` (`logs/`, `data/ingest.lock` 추가)
- Test: `tests/test_ingest_cli.py`

**Interfaces:**
- Consumes: `Container.build()`, `run_preflight`, `IngestRunner`, `build_notification`, `send_notification`, `IndexLedger`, `load_state`
- Produces:
  - `main(argv: list[str] | None = None) -> int` (`__main__.py`는 `raise SystemExit(main())`)
  - 하위 명령
    - `import-ledger <path>`: `ledger.import_from`, 이어서 "기록 파일 접수번호 N개 / Neo4j 공시 청크 접수번호 M개" 출력. Neo4j 연결이 안 되면 숫자 비교만 건너뛴다
    - `backfill [--since YYYY-MM-DD] [--corp CODE ...] [--types A,B,...]`: 기간 기본 오늘−2년~오늘, `advance_success_date=False`
    - `update`: `run_update()`
    - `status`: 마지막 실행 시각, 마지막 성공일, 재시도 대기 수, 재시도 중단 목록(접수번호·보고서명·오류)
  - `ledger_guard(ledger: IndexLedger, disclosure_chunk_count: int) -> str | None`: 기록이 비어 있고 수가 0보다 크면 `"기록 파일이 비어 있는데 Neo4j에 공시 청크가 있습니다. 먼저 python -m app.ingest import-ledger <Spring 기록 파일>을 실행하세요"`
  - `count_disclosure_chunks(driver) -> int`: 라벨이 `CompanyReportChunk_`로 시작하고 `metadata.doc_type`이 없는 노드 수
  - `prune_logs(log_dir: Path, today: date, keep_days: int = 30) -> int`: 이름이 `YYYY-MM-DD.log`이고 `today - keep_days`보다 오래된 파일 삭제, 지운 수 반환
  - 로그 설정: `backfill`·`update`는 `logs/ingest/<오늘>.log`에 `FileHandler`(추가 모드)와 콘솔 출력
  - 종료 코드: 정상·일부 실패 0, 점검 실패·가드 거부·중단·예상 못 한 예외 1, 실행 중(`IngestBusyError`) 0과 로그 한 줄
  - `backfill`·`update`의 공통 흐름: 점검 → 가드 → 실행 → 알림. 점검 실패·가드 거부·예상 못 한 예외도 `("공시 수집 실패", <한 줄>)` 알림

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_ledger_guard_refuses_empty_ledger_with_existing_chunks(tmp_path):
    assert "import-ledger" in ledger_guard(IndexLedger(tmp_path / "l.json"), 28362)

def test_ledger_guard_allows_fresh_install(tmp_path):
    assert ledger_guard(IndexLedger(tmp_path / "l.json"), 0) is None

def test_prune_logs_keeps_last_30_days(tmp_path):
    for name in ["2026-10-20.log", "2026-10-22.log", "notes.txt"]:
        (tmp_path / name).write_text("")
    assert prune_logs(tmp_path, date(2026, 11, 21)) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-22.log", "notes.txt"]

def test_parse_backfill_args():
    ns = build_parser().parse_args(["backfill", "--since", "2025-01-01", "--corp", "00126380", "--types", "A,I"])
    assert ns.since == date(2025, 1, 1) and ns.corp == ["00126380"] and ns.types == ["A", "I"]
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: `cli.py`, `__main__.py`, `.gitignore` 구현** (`build_parser() -> argparse.ArgumentParser`도 `cli.py`에 둔다)
- [ ] **Step 4: 통과 확인** — `.venv/bin/pytest -q` 전체 통과, `.venv/bin/python -m app.ingest --help`에 하위 명령 4개 표시
- [ ] **Step 5: Commit** — `git commit -m "feat(ingest): python -m app.ingest CLI (backfill/update/status/import-ledger)"`

### Task 9: launchd 설치 스크립트와 문서

**Files:**
- Create: `scripts/launchd/com.daniel0915.rag-company-report.ingest.plist.template`, `scripts/launchd/install.sh`, `scripts/launchd/uninstall.sh`
- Modify: `README.md` (실행 방법에 "공시 수집" 절 추가, 참고에 "수집은 이 프로젝트에서만" 추가, API 표의 `/index` 설명 수정)

**Interfaces:**
- plist 템플릿: `Label` `com.daniel0915.rag-company-report.ingest`, `ProgramArguments` `[__PROJECT__/.venv/bin/python, -m, app.ingest, update]`, `WorkingDirectory` `__PROJECT__`, `StartCalendarInterval` `{Weekday: 6, Hour: 7, Minute: 10}`, `StandardOutPath`/`StandardErrorPath` `__PROJECT__/logs/ingest/launchd.log`, `EnvironmentVariables.PATH` `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`
- `install.sh [--dry-run]`: 스크립트 위치에서 프로젝트 경로를 계산해 `__PROJECT__`를 채우고 `plutil -lint`로 검사한다. `--dry-run`이면 결과 plist를 출력만 한다. 아니면 `~/Library/LaunchAgents/`에 복사하고 `launchctl bootout`(있으면) 후 `launchctl bootstrap gui/$(id -u)`
- `uninstall.sh`: `launchctl bootout` 후 plist 삭제
- README "공시 수집" 절: 처음 실행 순서(설계 문서 "처음 실행 순서" 1~4), 전제 조건(Docker Desktop·Ollama 로그인 시 자동 실행, `docker update --restart unless-stopped company-report-neo4j`), `status` 명령, 로그 위치

- [ ] **Step 1: 템플릿과 스크립트 작성**
- [ ] **Step 2: 검증** — Run: `bash scripts/launchd/install.sh --dry-run | plutil -lint -` → Expected: `<stdin>: OK`. 출력에 `<integer>6</integer>`, `<integer>7</integer>`, `<integer>10</integer>`과 이 저장소의 절대 경로가 들어 있다
- [ ] **Step 3: README 수정**
- [ ] **Step 4: Commit** — `git commit -m "docs(ingest): launchd 주간 수집 설치 스크립트와 README"`

### Task 10: 실제 데이터로 확인 (삼성전자)

코드 변경 없음. Neo4j, Ollama가 켜져 있어야 한다. 설계 문서의 완료 기준 1~3을 확인한다.

- [ ] **Step 1: 기록 파일 가져오기** — Run: `.venv/bin/python -m app.ingest import-ledger ../rag-company-report/data/company-report-index.json` → Expected: 키 4,959개 가져옴, 기록 파일 접수번호 4,925개 / Neo4j 공시 청크 접수번호 수 출력
- [ ] **Step 2: 실행 전 삼성전자 청크 수 기록** — `MATCH (n:CompanyReportChunk_00126380) RETURN count(n)`
- [ ] **Step 3: 삼성전자만 backfill** — Run: `.venv/bin/python -m app.ingest backfill --corp 00126380` → Expected: 기존 A·D 공시는 `skipped`, B·C·E·I·J와 2026-08-31 이후 A·D가 `new`. 실행 후 청크 수 − 실행 전 청크 수 == 요약의 `chunks`
- [ ] **Step 4: 다시 실행** — 같은 명령 → Expected: 신규 0건
- [ ] **Step 5: Neo4j 끈 상태로 update** — `docker stop company-report-neo4j` 후 `.venv/bin/python -m app.ingest update` → Expected: 종료 코드 1, macOS 알림 "공시 수집 실패", `data/ingest-state.json`의 마지막 성공일 그대로. 확인 후 `docker start company-report-neo4j`
- [ ] **Step 6: 정정 관계 확인** — `MATCH (c:Report)-[:CORRECTS]->(o:Report) RETURN c.report_nm, o.report_nm LIMIT 5` → 삼성전자에 정정 공시가 있으면 행이 나온다

전체 22곳 backfill(약 8시간)과 `scripts/launchd/install.sh` 실행은 사용자가 정한 시점에 한다. 이 계획의 범위는 삼성전자 확인까지다.
