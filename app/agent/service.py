"""에이전트 방식 답변. provider에 따라 도구 루프(로컬 Ollama, DeepSeek API) 또는 Claude CLI(MCP)로 돌리고,
도구 기록으로 응답의 sources/toolCalls를 만든다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.agent.claude_agent import run_claude_agent
from app.agent.context import open_tool_context
from app.agent.executor import ToolExecutor
from app.agent.loop import OllamaStepModel, OpenAIStepModel, run_tool_loop
from app.agent.prompts import build_agent_system_prompt
from app.agent.tools import build_tools
from app.agent.trace import ToolCallRecord
from app.config import Settings
from app.llm.ollama import OllamaClient
from app.models import ChatResponse, ChatTurnDto, SourceItem, ToolCallTrace
from app.services.company_chat import CompanyChatService, _normalize_provider
from app.watchlist import find

SUMMARY_LENGTH = 200


@dataclass(frozen=True)
class AgentResult:
    answer: str
    records: list[ToolCallRecord]
    prompt_tokens: int | None


class AgentService:
    def __init__(
        self, settings: Settings, chat: CompanyChatService, ollama_agent: OllamaClient
    ) -> None:
        self._settings = settings
        self._chat = chat
        self._ollama_agent = ollama_agent

    def run(self, question: str, corp_code: str, provider: str | None) -> AgentResult:
        company = find(corp_code)
        system_prompt = build_agent_system_prompt(
            company.name if company else corp_code, corp_code, date.today()
        )
        llm = _normalize_provider(provider)
        if llm == "claude":
            result = run_claude_agent(question, system_prompt, corp_code, self._settings)
            return AgentResult(result.answer, result.records, None)
        model = (
            OpenAIStepModel(self._chat.deepseek_client())
            if llm == "deepseek"
            else OllamaStepModel(self._ollama_agent)
        )

        ctx, close = open_tool_context(self._settings, corp_code)
        try:
            executor = ToolExecutor(
                build_tools(ctx), max_calls=self._settings.agent_max_tool_calls
            )
            loop = run_tool_loop(model, executor, system_prompt, question)
            return AgentResult(loop.answer, executor.records, loop.prompt_tokens)
        finally:
            close()

    def chat(
        self,
        question: str,
        corp_code: str,
        history: list[ChatTurnDto],
        provider: str | None,
    ) -> ChatResponse:
        if not corp_code or not corp_code.strip():
            raise ValueError("corpCode는 필수입니다")
        agent_question = (
            self._chat.rewrite_query(question, history, _normalize_provider(provider))
            if history
            else question
        )
        result = self.run(agent_question, corp_code, provider)
        return ChatResponse(
            answer=result.answer,
            sources=_collect_sources(result.records),
            toolCalls=[
                ToolCallTrace(
                    name=r.name,
                    arguments=r.arguments,
                    ok=r.ok,
                    summary=r.result_text[:SUMMARY_LENGTH],
                )
                for r in result.records
            ],
        )


def _collect_sources(records: list[ToolCallRecord]) -> list[SourceItem]:
    """호출 순서대로 모으되 같은 출처는 한 번만 둔다."""
    items: list[SourceItem] = []
    for record in records:
        for source in record.sources:
            if source.get("kind") == "web":
                # exclude_unset 응답에서도 키가 빠지지 않도록 None 필드를 명시한다.
                item = SourceItem(
                    corpName=None,
                    reportNm=source.get("title"),
                    rceptNo=source.get("url"),
                    sectionTitle=None,
                    docType="WEB",
                )
            else:
                item = SourceItem(
                    corpName=source.get("corp_name"),
                    reportNm=source.get("report_nm"),
                    rceptNo=source.get("rcept_no"),
                    sectionTitle=source.get("section_title"),
                    docType=source.get("doc_type"),
                )
            if item not in items:
                items.append(item)
    return items
