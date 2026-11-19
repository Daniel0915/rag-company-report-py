# RAG 답변 품질 평가(Eval) 설계

작성일: 2026-11-19 · 대상 저장소: `rag-company-report-py`

## 왜 만드나

기업 리포트 챗봇의 답변 품질을 올리기 위해서다. 목표는 두 가지다.

- **할루시네이션을 줄인다.** 공시에 없는 내용을 지어내지 않고, 답이 없으면 모른다고 한다.
- **정확한 정보를 준다.** 숫자, 기간, 단위, 사실이 공시와 일치한다.

평가 점수는 반복 개선에 쓴다. 설정이나 프롬프트를 하나 바꾸고 평가를 돌려 점수가 오르는지 확인한다. 이미 알려진 개선 후보는 다음과 같다.

- Ollama `num_ctx` 미설정으로 입력이 약 2K 토큰에서 잘리는 문제. 실제 측정에서 `prompt_eval_count`가 2,050이었다.
- topK, 청크 크기와 겹침, 리랭커, 시스템 프롬프트, 답변 모델

모델 가중치를 바꾸는 Post-training은 하지 않는다. 이 평가는 고정된 모델 위에서 검색과 프롬프트 파이프라인을 잰다.

## 목표, 비목표, 반목표

**목표**
1. 고정 정답셋으로 검색과 답변을 채점한다.
2. 실행 결과를 파일로 남기고, 두 실행을 비교한다.
3. 정답셋 대부분을 자동 생성하고 사람이 검수한다.
4. 검색 실패와 생성 실패를 구분할 수 있게 단계별 결과를 남긴다.

**비목표**
- 이번 작업에서 `num_ctx`, 리랭커 같은 품질 개선 자체를 하지 않는다. 각 개선은 이 평가 위에서 별도로 진행한다.
- 관리자 화면, API 엔드포인트, CI 연동은 만들지 않는다.
- 대화 히스토리(후속 질문 재작성)는 평가하지 않는다. 모든 문항은 단일 질문이다.
- 뉴스(`doc_type=NEWS`)는 평가하지 않는다. 뉴스는 계속 새로 수집돼 정답이 고정되지 않는다.

**반목표** (기술적으로는 돌아가도 실패로 보는 경우)
- 평가 도입으로 기존 채팅 API의 동작이나 응답 형식이 바뀌는 것
- `ANTHROPIC_API_KEY` 같은 종량제 API 키를 쓰거나 요구하는 것
- 점수만 오르고 실제 품질은 그대로인 것. 예를 들어 무엇이든 "모른다"고 답해 거절 점수만 올리는 경우를 잡을 수 있어야 한다. 그래서 답이 있는 문항에서 잘못 거절한 비율도 함께 잰다.

## 제약

- Claude는 로그인된 `claude` CLI(`app/llm/claude_cli.py`)로만 호출한다(`CLAUDE.md`). CLI 기본 타임아웃은 60초(`CLAUDE_TIMEOUT_SECONDS`)이므로, 평가용 생성과 채점 호출은 별도로 더 긴 타임아웃(예: 180초)을 쓴다.
- 평가를 실행하려면 Neo4j와 Ollama가 켜져 있어야 한다. FastAPI 서버는 필요 없다.
- 공시가 색인된 기업은 9곳이다. 정답셋은 데이터가 있는 기업에서만 만든다. 삼성전자(`00126380`) 청크 구성은 다음과 같다(2026-11-19 확인).

  | 종류 | 청크 | 보고서 |
  |---|---|---|
  | 지분공시 (`pblntf_ty=D`) | 22,769 | 3,492 |
  | 정기공시 (`pblntf_ty=A`) | 4,833 | 7 |
  | 유형 없음 (예전 색인의 반기보고서) | 760 | 1 |
  | 뉴스 (`doc_type=NEWS`) | 126 | 40 |

- 청크 ID는 재색인하면 바뀐다. 정답 근거는 청크 ID가 아니라 **공시 섹션**으로 지정한다. 섹션 키는 `(rcept_no, source_file, section_order)`이고, `section_title`을 함께 저장한다. 공시 하나의 zip에 본문과 감사보고서 같은 XML 파일이 여럿 들어 있고, 파일마다 `section_order`가 0부터 다시 시작하기 때문에 `source_file`이 필요하다. 삼성전자 데이터에서 `(rcept_no, section_order)`만으로는 12개 섹션이 겹친다.

## 접근 방식

정답셋, 채점, 실행 형태 세 가지를 정했다.

| 결정 | 고려한 방식 | 선택과 이유 |
|---|---|---|
| 정답셋 | A. 직접 작성 / **B. 자동 생성 후 검수** / C. 실제 질문 수집 | **B**. 근거 섹션이 생성 시점에 확정돼 검색 채점이 정확하다. 단, 답이 없는 질문은 자동으로 만들기 어려워 **사용자가 직접 쓴다**. C는 대화 기록이 브라우저에만 있어 모을 데이터가 없다. |
| 채점 | A. Claude CLI 채점 / B. 로컬 qwen 채점 / **C. 규칙 채점** | **C를 기본, A를 옵션**으로 둔다. 매번은 빠르고 결과가 같은 규칙 채점으로 돌리고, 정밀 확인이 필요할 때만 Claude 채점을 켠다. B는 3B 모델이라 채점이 불안정하고, 자기 답을 자기가 채점하게 된다. |
| 실행 형태 | **A. CLI + 파일** / B. 관리자 API + 화면 / C. pytest | **A**. 반복 실행과 비교에 가장 맞고 VS Code에서 바로 쓴다. B는 프론트엔드 소스가 다른 저장소에 있고 긴 작업에 백그라운드 처리가 필요하다. C는 통과/실패만 남아 점수 변화를 볼 수 없다. |

## 사용 흐름

```bash
# 1) 후보 문항 자동 생성 (Claude CLI 사용)
python -m app.eval generate --corp 00126380 -n 30
#    → eval/candidates.yaml 에 추가된다

# 2) VS Code에서 eval/candidates.yaml 검수
#    문항을 고치고, 쓸 문항은 review: ok, 버릴 문항은 review: drop 으로 바꾼다
#    답 없는 질문은 eval/dataset.yaml 에 직접 추가한다

# 3) 검수한 문항을 정답셋으로 옮기기
python -m app.eval accept
#    → review: ok 인 문항을 eval/dataset.yaml 로 옮기고 candidates.yaml 에서 지운다

# 4) 평가 실행
python -m app.eval run --provider local --top-k 6            # 규칙 채점만
python -m app.eval run --provider local --top-k 6 --judge claude
#    → eval/runs/<시각>_<provider>_k<topK>.json + 터미널 요약표

# 5) 두 실행 비교
python -m app.eval compare eval/runs/A.json eval/runs/B.json
#    → 지표별 변화와, 통과에서 실패로 바뀐 문항 목록
```

## 정답셋 형식 (`eval/dataset.yaml`)

```yaml
- id: q0001
  corp_code: "00126380"
  question: 삼성전자 2024년 연결 기준 매출액은 얼마야?
  answerable: true
  expected_answer: 2024년 연결 기준 매출액은 300조 8,709억원이다.
  must_include: ["300조 8,709억"]        # 규칙 채점용 핵심 사실. 숫자는 단위까지 쓴다
  expected_sections:
    - rcept_no: "20250311001085"
      source_file: "20250311001085.xml"
      section_order: 12
      section_title: "1. 사업의 개요"     # 상위 장(II. 사업의 내용) 이름은 저장되지 않는다
  origin: generated                      # generated | manual

- id: m0001
  corp_code: "00126380"
  question: 삼성전자의 2030년 예상 배당금은?
  answerable: false                      # 공시에 답이 없는 문항
  origin: manual
```

예시의 숫자와 접수번호는 형식을 보이기 위한 것이다.

- `answerable: false`이면 `must_include`와 `expected_sections`는 쓰지 않는다.
- `candidates.yaml`은 위 형식에 `review: pending | ok | drop`과 생성 근거인 `source_chunk_text`가 더 붙는다. `source_chunk_text`는 검수할 때 원문을 보기 위한 것이고, `accept` 때 지운다.

## 자동 생성 (`generate`)

1. 대상 기업의 공시 청크(`doc_type`이 없는 DART 청크) 중 무작위로 N개를 고른다. 너무 짧은 청크(예: 200자 미만)는 뺀다. 한 섹션에서 두 번 뽑지 않는다.
2. 청크마다 Claude CLI에 다음을 요청한다. "이 발췌만으로 답할 수 있는 질문 하나와 답, 핵심 사실을 JSON으로 써라. 질문은 원문 문장을 베끼지 말고 투자자가 실제로 묻는 말투로 써라."
3. 응답 JSON을 파싱해 `candidates.yaml`에 추가한다. 근거 섹션은 뽑은 청크의 `(rcept_no, section_order, section_title)`이다. 파싱에 실패한 청크는 건너뛰고 개수를 출력한다.
4. 같은 기업에 다시 실행하면, 이미 정답셋이나 후보에 있는 섹션은 피해서 뽑는다. `--seed`로 무작위 선택을 재현할 수 있다.

## 실행 (`run`)

문항마다 실제 채팅과 같은 경로를 탄다. 단, 대화 히스토리는 없다.

1. **검색:** `CompanyChatService`에서 떼어 낸 검색 단계로 상위 topK 청크를 가져온다.
2. **답변:** 같은 서비스의 답변 단계로 그래프 보강과 시스템 프롬프트를 거쳐 선택한 provider가 답한다.
3. **채점:** 규칙 채점을 하고, `--judge claude`이면 Claude 채점을 더한다.
4. 한 문항에서 오류가 나도 전체 실행을 멈추지 않는다. 그 문항에 오류를 기록하고 넘어간다.

### 결과 흔들림 줄이기

지금 `OllamaClient`는 temperature와 seed를 지정하지 않아서, 아무것도 안 바꿔도 답변이 매번 달라진다. 로컬 provider는 문항마다 Ollama를 두 번 부르고(Text2Cypher 라우터, 답변), 두 호출 모두 흔들린다. 이대로면 `compare`가 우연히 바뀐 문항을 "나빠졌다"고 보고한다.

그래서 `run`은 기본으로 **temperature 0, seed 고정**으로 Ollama를 부른다(설계자 판단).
- 이 값은 평가 실행에만 적용하고, 채팅 API의 동작은 바꾸지 않는다. `OllamaClient`에 호출별 옵션을 넘길 수 있는 선택 인자를 추가하고, 기본값은 지금과 같게 둔다.
- 실제 운영 설정과 같은 조건으로 재고 싶으면 `--temperature`와 `--seed`로 바꿀 수 있다. 실제로 쓴 값은 결과 파일의 실행 설정에 남긴다.
- 운영 채팅의 temperature를 낮추는 것 자체는 평가 위에서 따로 실험할 개선 후보다.
- Gemini와 Claude CLI는 seed를 고정할 수 없다. 이 provider들의 비교 결과는 흔들림을 감안해 읽는다.

### 필요한 코드 변경

`CompanyChatService.chat()`(`app/services/company_chat.py:110`)을 공개 메서드 두 개로 나눈다.
- `retrieve(question, corp_code, top_k) -> list[Document]`: 뉴스 키워드 필터를 포함한 지금의 검색 로직
- `answer(question, corp_code, context, provider) -> str`: 그래프 보강, 시스템 프롬프트 조립, provider 호출

`chat()`은 재작성, `retrieve`, `answer`를 차례로 부르는 형태가 되고, 동작과 응답은 그대로다.

Ollama가 실제로 읽은 입력 토큰 수(`prompt_eval_count`)를 평가 결과에 남길 수 있도록 `OllamaClient`가 이 값을 함께 돌려줄 방법을 만든다. 예를 들어 메타데이터를 돌려주는 메서드를 하나 더 두는 방식이다. 기존 `generate()`의 반환형은 바꾸지 않는다. 입력 잘림(`num_ctx`) 문제를 숫자로 보이게 하려는 목적이다.

## 채점 지표

### 규칙 채점 (기본, 매 실행)

| 지표 | 대상 문항 | 정의 |
|---|---|---|
| `hit@k` (엄격) | 답 있음 | 검색된 topK 청크 중 하나라도 기대 섹션 `(rcept_no, source_file, section_order)`에 속하면 1 |
| `hit@k` (느슨) | 답 있음 | 기대 섹션과 `section_title`이 같은 섹션이 같은 기업의 다른 보고서에서 검색돼도 1. 사업/반기/분기 보고서에 같은 섹션이 반복되기 때문이다. |
| `mrr` | 답 있음 | 기대 섹션(엄격)이 처음 나온 순위의 역수. 없으면 0 |
| `fact_recall` | 답 있음 | `must_include` 중 답변에 들어 있는 비율. 공백과 쉼표를 지우고 비교한다. |
| `false_refusal` | 답 있음 | 답이 있는데 거절 문구로 답한 비율 |
| `correct_refusal` | 답 없음 | 거절 문구로 답한 비율. 높을수록 좋다. |
| `unsupported_numbers` | 전체 | 답변 속 숫자 중, 검색된 근거 텍스트 어디에도 없는 숫자의 수. 지어낸 숫자를 잡는 값싼 신호다. 연도처럼 질문에 들어 있던 숫자는 뺀다. |
| `prompt_tokens` | 전체 (local만) | Ollama가 실제로 읽은 입력 토큰 수 |

거절 문구는 목록으로 관리한다. 예: "모릅니다", "알 수 없", "확인할 수 없", "찾을 수 없", "포함되어 있지 않", "정보가 없".

### Claude 채점 (옵션, `--judge claude`)

문항마다 질문, 검색된 근거, 답변, 기대 답을 주고 JSON으로 받는다.
- `faithfulness` (0~1): 답변의 주장 중 근거로 뒷받침되는 비율. 할루시네이션의 직접 지표다.
- `correct` (true/false): 기대 답과 의미가 일치하는가. 답 없는 문항은 적절히 거절했는가.
- `reason`: 한두 문장의 판단 이유

답변 provider가 `claude`인데 채점도 `claude`이면 경고를 출력한다. 자기 답을 자기가 채점하게 되기 때문이다.

## 결과 파일 (`eval/runs/*.json`)

- **실행 설정:** provider, topK, 채팅·임베딩 모델 이름, 채점 방식, 정답셋 파일 해시, git 커밋, 실행 시각
- **문항별 결과:** 질문, 검색된 청크(섹션 키·제목·유사도 점수), 답변, 각 지표 값, Claude 채점 결과, 오류
- **전체 지표:** 각 지표의 평균과 분모(대상 문항 수)

터미널 요약표에는 지표별 값과 분모를 출력한다.

## 비교 (`compare`)

- 두 결과 파일의 전체 지표를 나란히 놓고 차이를 보여 준다.
- 같은 `id` 문항끼리 맞춰서, 좋아진 문항과 나빠진 문항을 나열한다. 기준은 `hit@k`(엄격), `fact_recall`, 거절 판정, (있으면) Claude `correct`이다.
- 두 실행의 정답셋 해시가 다르면 경고한다.

## 파일 배치

```
app/eval/                 # 평가 패키지 (python -m app.eval)
  __main__.py             # generate / accept / run / compare 하위 명령
  dataset.py              # 정답셋·후보 YAML 읽기/쓰기, 형식 검증
  generate.py             # 청크 샘플링 + Claude 질문 생성
  runner.py               # 문항 실행 (검색 → 답변 → 채점)
  metrics.py              # 규칙 채점 (순수 함수)
  judge.py                # Claude 채점
  compare.py              # 두 실행 비교
eval/
  dataset.yaml            # 확정 정답셋 (커밋)
  candidates.yaml         # 검수 대기 후보 (커밋)
  runs/                   # 실행 결과 (.gitignore)
tests/test_eval_*.py      # 규칙 채점, 정답셋 파싱, 비교 로직 단위 테스트
```

- YAML 처리를 위해 `pyyaml`을 의존성에 추가한다.
- 단위 테스트는 LLM과 Neo4j 없이 돈다. 채점과 비교는 순수 함수로 만든다.

## 빌더에게 맡기는 것

- 생성과 채점 프롬프트의 정확한 문구
- 숫자 추출과 정규화 규칙의 세부(예: "1,234억", "12.5%", "3조")
- 터미널 요약표 형식, 결과 파일 이름 규칙의 세부
- 거절 문구 목록의 초기 구성

## 처음 할 일 (완료 기준)

1. 삼성전자로 후보 30개를 생성하고, 사용자가 검수해 20개 이상을 정답셋에 넣는다. 사용자가 답 없는 질문 5~10개를 직접 쓴다.
2. `--provider local --top-k 6`으로 첫 기준선을 실행하고 결과 파일을 남긴다.
3. 같은 설정(temperature 0, 같은 seed)으로 한 번 더 돌려 `compare`를 실행한다. 검색 지표는 완전히 같아야 한다. 답변 지표가 바뀐 문항이 전체의 10%를 넘으면, 그 원인을 찾은 뒤에 이 결과를 기준선으로 삼는다.
