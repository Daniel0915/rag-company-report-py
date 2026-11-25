"""
DART 공시 원문(사업/반기/분기보고서 + 지분공시)과 뉴스를 근거로 기업 정보를 답변하는 채팅.
기업마다 벡터 인덱스가 분리돼 있어 다른 기업 데이터가 애초에 검색 후보에 섞이지 않는다.
벡터 검색으로는 안 되는 지분 관계(제출인)는 DisclosureGraphService의 그래프에서 보강한다.

그래프 보강은 집계/랭킹/교집합처럼 고정 조회 하나로 못 푸는 질문일 때, 답변에 쓰기로
선택된 provider(local/claude)가 그 자리에서 Cypher를 생성해(Text2Cypher) 읽기 전용으로
실행하는 방식으로 확장된다. 생성 실패/쓰기 구문 포함/실행 오류 시에는 벡터 검색으로 이미
뽑힌 청크(rcept_no/article_url)에 앵커된 고정 Cypher(VectorCypher)로 안전하게 폴백한다.

대화 히스토리(프론트 IndexedDB 보관분)가 있으면, 후속 질문("만원이 맞아?" 같은 키워드 없는
질문)을 그대로 임베딩하지 않고 먼저 독립형 질문으로 재작성한 뒤 그 결과로 벡터 검색을 한다 -
안 그러면 후속 질문마다 검색이 엉뚱하게 튄다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.llm.claude_cli import ClaudeCliClient
from app.llm.ollama import OllamaClient
from app.models import ChatResponse, ChatTurnDto, GraphExpansionRow, SourceItem
from app.services.disclosure_graph import DisclosureGraphService
from app.vector.document import Document
from app.vector.registry import CompanyVectorStoreRegistry

log = logging.getLogger(__name__)

REWRITE_PROMPT_TEMPLATE = """아래는 사용자와 어시스턴트의 이전 대화입니다.

{history}

위 대화 맥락을 참고해서, 아래 "최신 질문"이 이 대화만 보고도 무엇을 묻는
것인지 알 수 있는 완전한 질문 하나로 다시 쓰세요. 이전 대화에서 이미 나온
기업명/보고서명/항목명이 있다면 그걸 명시적으로 포함하세요. 설명이나 답변
없이 재작성된 질문 문장 하나만 출력하세요.

최신 질문: {question}
"""

CYPHER_ROUTER_PROMPT_TEMPLATE = """당신은 Neo4j 읽기 전용 Cypher 쿼리를 작성하는 어시스턴트입니다. 아래 그래프
스키마로 답할 수 있는, 지분공시 제출인 관계에 대한 집계·랭킹·비교·교집합·
역방향 조회 같은 그래프 탐색이 질문에 필요하면 그 질문에 맞는 Cypher 쿼리
하나만 출력하세요 (MATCH, WHERE, RETURN, ORDER BY, LIMIT, count()만 사용하고
CREATE/MERGE/SET/DELETE/REMOVE/DROP/LOAD CSV는 절대 쓰지 마세요). 회사 소개나
재무/사업내용처럼 그래프 탐색이 필요 없는 질문이면 다른 설명 없이 정확히
NONE 이라고만 출력하세요. 쿼리든 NONE이든 그 외 설명은 절대 덧붙이지 마세요.

스키마:
(:Company {{corp_code, name, stock_code}})
(:Report {{rcept_no, report_nm, rcept_dt, pblntf_ty}})
(:Filer {{name}})
(:Article {{url, title, published_date}})
(:Media {{name}})
(:Report)-[:FILED_BY]->(:Company)
(:Filer)-[:DISCLOSED]->(:Report)
(:Article)-[:ABOUT]->(:Company)
(:Media)-[:PUBLISHED]->(:Article)

현재 대상 기업의 corp_code: '{corp_code}'

질문: {question}
"""

SYSTEM_PROMPT_TEMPLATE = """당신은 DART 전자공시 자료를 근거로 기업 정보를 설명하는 어시스턴트입니다.
아래 context는 해당 기업이 금융감독원에 제출한 사업보고서/반기보고서/분기보고서
원문에서 발췌한 내용이고, 그래프 정보는 지분공시(주식등의대량보유상황보고서,
임원ㆍ주요주주소유보고서)에서 추출한 제출인 관계입니다. 이 정보들만 근거로
답변하고, 없는 내용이면 모른다고 답하세요. 답변에는 근거가 된 공시명과 섹션을
함께 언급하세요. 한국어로 답변하세요. 뉴스 목록을 요청받으면 제목, 출처, 날짜,
링크만 간결하게 나열하고, 관련성 여부에 대한 부연 설명이나 주석은 달지 마세요.

context:
{context}

그래프 정보 (지분공시 제출인 관계 및 뉴스 기사의 다른 기업 언급):
{graph}
"""

WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|LOAD\s+CSV|CALL\s+apoc)\b", re.IGNORECASE
)

# 벡터 검색은 doc_type 구분 없이 코사인 유사도로만 후보를 뽑기 때문에, "뉴스 알려줘"처럼
# 뉴스 자체를 가리킬 뿐 구체적인 내용이 없는 질문은 임베딩이 모호해서 공시 청크가 더
# 가깝게 나오는 경우가 있다. 질문에 이 키워드가 있으면 doc_type='NEWS'로 검색을 좁힌다.
NEWS_KEYWORD = re.compile("뉴스|기사|언론")
NEWS_FILTER_CANDIDATE_POOL = 200
VECTOR_CYPHER_LIMIT = 30

CODE_FENCE = re.compile(r"```(?:cypher)?", re.DOTALL)


@dataclass(frozen=True)
class Answer:
    text: str
    # provider가 local(Ollama)일 때만 채워지는 입력 토큰 수.
    prompt_tokens: int | None


class CompanyChatService:
    def __init__(
        self,
        ollama: OllamaClient,
        claude: ClaudeCliClient,
        registry: CompanyVectorStoreRegistry,
        graph_service: DisclosureGraphService,
    ) -> None:
        self._ollama = ollama
        self._claude = claude
        self._registry = registry
        self._graph = graph_service

    def chat(
        self,
        question: str,
        corp_code: str,
        top_k: int,
        history: list[ChatTurnDto],
        provider: str | None,
    ) -> ChatResponse:
        """provider: "local"(Ollama, 기본값) 또는 "claude"(claude CLI)."""
        if not corp_code or not corp_code.strip():
            raise ValueError("corpCode는 필수입니다")
        llm = _normalize_provider(provider)

        search_query = self.rewrite_query(question, history, llm) if history else question
        context = self.retrieve(search_query, corp_code, top_k, news_query=question)
        answer = self.answer(question, corp_code, context, provider, search_query=search_query)

        sources = [
            SourceItem(
                corpName=_meta_str(d, "corp_name"),
                reportNm=_meta_str(d, "report_nm"),
                rceptNo=_meta_str(d, "rcept_no"),
                sectionTitle=_meta_str(d, "section_title"),
                docType=d.metadata.get("doc_type"),
            )
            for d in context
        ]
        return ChatResponse(answer=answer.text, sources=sources)

    def retrieve(
        self,
        question: str,
        corp_code: str,
        top_k: int,
        news_query: str | None = None,
        *,
        doc_type: str | None = None,
    ) -> list[Document]:
        """
        벡터 검색 단계. 임베딩은 question으로, 뉴스 키워드 판정은 news_query(없으면 question)로 한다 -
        chat()은 재작성된 질문으로 검색하되 뉴스 여부는 사용자가 쓴 원래 질문으로 판정하기 때문이다.
        doc_type이 주어지면(에이전트 도구) 키워드 판정과 상관없이 그 값으로 필터를 건다.
        """
        if doc_type is not None:
            metadata_filter = {"doc_type": doc_type}
        elif NEWS_KEYWORD.search(news_query if news_query is not None else question):
            metadata_filter = {"doc_type": "NEWS"}
        else:
            metadata_filter = None
        news_only = metadata_filter is not None
        # 메타데이터 필터는 ANN 검색 이후 후보군(topK개)에 대한 WHERE절로 적용된다 -
        # topK를 그대로 쓰면 상위 후보에 NEWS 청크가 하나도 안 걸려 필터링 후 0건이 되기 쉽다.
        # 필터가 걸릴 땐 후보군을 넉넉히 가져온 뒤 잘라낸다.
        ann_top_k = max(top_k, NEWS_FILTER_CANDIDATE_POOL) if news_only else top_k

        context = self._registry.for_company(corp_code).similarity_search(
            query=question,
            top_k=ann_top_k,
            metadata_filter=metadata_filter,
        )
        if news_only and len(context) > top_k:
            context = context[:top_k]
        return context

    def answer(
        self,
        question: str,
        corp_code: str,
        context: list[Document],
        provider: str | None,
        search_query: str | None = None,
    ) -> Answer:
        """답변 단계: 그래프 보강, 시스템 프롬프트 조립, provider 호출. local이면 입력 토큰 수도 채운다."""
        llm = _normalize_provider(provider)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            context=_build_context_text(context),
            graph=self._build_graph_text(corp_code, search_query or question, llm, context),
        )
        if llm == "local":
            reply = self._ollama.chat(system_prompt, question)
            return Answer(text=reply.content, prompt_tokens=reply.prompt_eval_count)
        return Answer(text=self._complete(llm, system_prompt, question), prompt_tokens=None)

    def rewrite_query(self, question: str, history: list[ChatTurnDto], llm: str) -> str:
        history_text = "\n".join(
            ("사용자: " if turn.role == "user" else "어시스턴트: ") + turn.content
            for turn in history
        )
        rewritten = self._complete(
            llm, "", REWRITE_PROMPT_TEMPLATE.format(history=history_text, question=question)
        )
        return rewritten.strip() if rewritten and rewritten.strip() else question

    def _complete(self, llm: str, system_prompt: str, user_prompt: str) -> str:
        if llm == "claude":
            return self._claude.generate(system_prompt, user_prompt)
        return self._ollama.generate(system_prompt, user_prompt)

    def _build_graph_text(
        self, corp_code: str, question: str, llm: str, context: list[Document]
    ) -> str:
        """
        질문이 집계/랭킹/교집합처럼 고정 조회 하나로는 못 푸는 그래프 탐색을 요구하면,
        선택된 provider에게 그 자리에서 Cypher를 생성시켜 읽기 전용으로 실행한다(Text2Cypher).
        생성이 안 되거나(NONE), 쓰기 구문이 섞여 있거나, 실행이 실패/빈 결과면
        VectorCypher 확장으로 폴백한다.
        """
        cypher = self._generate_graph_query(corp_code, question, llm)
        if cypher:
            log.info("[Text2Cypher] generated query: %s", cypher)
            try:
                result = self._graph.run_read_only_query(cypher)
                if result is not None:
                    log.info(
                        "[Text2Cypher] execution succeeded, %d char(s) of result used", len(result)
                    )
                    return result
                log.info("[Text2Cypher] query returned no rows, falling back to VectorCypher expansion")
            except Exception as e:  # noqa: BLE001 - 어떤 실행 오류든 폴백으로 흡수한다
                log.info(
                    "[Text2Cypher] execution failed (%s), falling back to VectorCypher expansion", e
                )
        else:
            log.info("[Text2Cypher] router returned NONE, using VectorCypher expansion")
        return self._vector_cypher_graph_text(corp_code, context)

    def _generate_graph_query(self, corp_code: str, question: str, llm: str) -> str | None:
        try:
            raw = self._complete(
                llm,
                "",
                CYPHER_ROUTER_PROMPT_TEMPLATE.format(corp_code=corp_code, question=question),
            )
        except Exception as e:  # noqa: BLE001 - 라우터 호출 실패도 폴백으로 흡수한다
            log.info("[Text2Cypher] router call failed (%s)", e)
            return None
        if raw is None:
            return None
        cypher = CODE_FENCE.sub("", raw).strip()
        if not cypher or cypher.upper() == "NONE" or WRITE_CLAUSE.search(cypher):
            return None
        return cypher

    def _vector_cypher_graph_text(self, corp_code: str, context: list[Document]) -> str:
        """
        VectorCypher: 벡터 검색으로 이미 뽑힌 topK 청크 중 지분공시(D타입)/뉴스 청크의
        rcept_no/article_url만 모아 그래프 확장에 넘긴다. 이번 검색 결과와 무관한
        "기업 전체 최근 제출인" 대신, 실제로 근거로 쓰인 공시/기사와 연결된 정보만 보강한다.
        """
        rcept_nos = _distinct(
            _meta_str(d, "rcept_no") for d in context if d.metadata.get("pblntf_ty") == "D"
        )
        article_urls = _distinct(
            _meta_str(d, "article_url") for d in context if d.metadata.get("doc_type") == "NEWS"
        )
        if not rcept_nos and not article_urls:
            log.info("[VectorCypher] no D-type/NEWS chunks in retrieved context, skipping graph expansion")
            return "(검색된 근거 청크에서 확장할 그래프 정보 없음)"

        rows = self._graph.expand_retrieved_context(
            corp_code, rcept_nos, article_urls, VECTOR_CYPHER_LIMIT
        )
        log.info(
            "[VectorCypher] expanded %d row(s) from %d rcept_no(s) / %d article_url(s)",
            len(rows),
            len(rcept_nos),
            len(article_urls),
        )
        if not rows:
            return "(검색된 근거 청크에 연결된 추가 그래프 정보 없음)"
        return _format_graph_expansion_rows(rows)


def _normalize_provider(provider: str | None) -> str:
    if provider == "claude":
        return "claude"
    return "local"


def _meta_str(document: Document, key: str) -> str:
    return str(document.metadata.get(key))


def _distinct(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v and v != "None"))


def _format_graph_expansion_rows(rows: list[GraphExpansionRow]) -> str:
    lines: list[str] = []
    for row in rows:
        if row.kind == "DISCLOSURE":
            line = f'- [지분공시] 제출인 {row.primaryName}이(가) 공시한 "{row.secondaryName}"'
            if row.relatedCorpCode:
                line += f" | 같은 제출인이 지분을 공시한 다른 기업: {row.relatedCorpName} ({row.relatedCorpCode})"
        else:
            line = (
                f'- [뉴스] "{row.secondaryName}" ({row.primaryName}) 기사는 '
                f"{row.relatedCorpName}에 대해서도 다룸"
            )
        lines.append(line)
    return "\n".join(lines) + "\n"


def _build_context_text(context: list[Document]) -> str:
    return "".join(
        f"[{i}] (출처: {d.metadata.get('corp_name')} {d.metadata.get('report_nm')} > "
        f"{d.metadata.get('section_title')}) {d.text}\n\n"
        for i, d in enumerate(context, start=1)
    )
