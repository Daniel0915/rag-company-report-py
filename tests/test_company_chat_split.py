from app.llm.ollama import OllamaReply
from app.models import ChatTurnDto
from app.services.company_chat import CompanyChatService
from app.vector.document import Document


def _docs(n: int = 3) -> list[Document]:
    return [
        Document(
            text=f"본문 {i}",
            metadata={
                "corp_name": "삼성전자",
                "report_nm": "사업보고서",
                "rcept_no": f"2024{i:04d}",
                "section_title": f"섹션 {i}",
            },
        )
        for i in range(n)
    ]


class FakeStore:
    def __init__(self, registry):
        self._registry = registry

    def similarity_search(self, query, top_k, metadata_filter=None):
        self._registry.calls.append(
            {"query": query, "top_k": top_k, "metadata_filter": metadata_filter}
        )
        return list(self._registry.docs)


class FakeRegistry:
    def __init__(self, docs):
        self.docs = docs
        self.calls: list[dict] = []

    def for_company(self, corp_code):
        return FakeStore(self)


class FakeGraph:
    def run_read_only_query(self, cypher):
        raise AssertionError("라우터가 NONE이면 호출되면 안 된다")

    def expand_retrieved_context(self, *args):
        raise AssertionError("D타입/뉴스 청크가 없으면 호출되면 안 된다")


class FakeOllama:
    def generate(self, system_prompt, user_prompt):
        if "Cypher" in user_prompt:
            return "NONE"
        return "삼성전자 뉴스 목록"  # 재작성 프롬프트

    def chat(self, system_prompt, user_prompt):
        return OllamaReply("답변", 1234)


class FakeClaude:
    def generate(self, system_prompt, user_prompt):
        return "클로드 답변"


def _service(docs):
    registry = FakeRegistry(docs)
    service = CompanyChatService(FakeOllama(), FakeClaude(), registry, FakeGraph())
    return service, registry


def test_retrieve_widens_pool_and_filters_news():
    service, registry = _service(_docs())
    docs = service.retrieve("삼성전자 뉴스 알려줘", "00126380", 6)
    assert registry.calls[-1]["top_k"] == 200
    assert registry.calls[-1]["metadata_filter"] == {"doc_type": "NEWS"}
    assert len(docs) <= 6


def test_retrieve_judges_news_on_news_query_but_embeds_question():
    service, registry = _service(_docs())
    service.retrieve("삼성전자 매출", "00126380", 6, news_query="뉴스 알려줘")
    assert registry.calls[-1]["query"] == "삼성전자 매출"
    assert registry.calls[-1]["metadata_filter"] == {"doc_type": "NEWS"}


def test_answer_reports_prompt_tokens_for_local_only():
    service, _ = _service(_docs())
    docs = _docs()
    assert service.answer("매출?", "00126380", docs, "local").prompt_tokens == 1234
    claude = service.answer("매출?", "00126380", docs, "claude")
    assert claude.prompt_tokens is None
    assert claude.text == "클로드 답변"


def test_chat_with_history_judges_news_on_original_question():
    service, registry = _service(_docs())
    service.chat("그거 매출은?", "00126380", 6, [ChatTurnDto(role="user", content="삼성전자")], "local")
    assert registry.calls[-1]["query"] == "삼성전자 뉴스 목록"
    assert registry.calls[-1]["metadata_filter"] is None


def test_chat_response_unchanged():
    docs = _docs()
    service, _ = _service(docs)
    resp = service.chat("매출?", "00126380", 6, [], "local")
    assert resp.answer == "답변"
    assert [s.rceptNo for s in resp.sources] == [d.metadata["rcept_no"] for d in docs]
