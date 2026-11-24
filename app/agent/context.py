"""도구 실행에 필요한 외부 연결(Neo4j, Ollama, DART, Tavily)을 만들고 닫는다."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from neo4j import GraphDatabase

from app.agent.tavily import TavilyClient
from app.agent.tools import ToolContext
from app.config import Settings
from app.dart.client import DartApiClient
from app.llm.claude_cli import ClaudeCliClient
from app.llm.ollama import OllamaClient
from app.services.company_chat import CompanyChatService
from app.services.disclosure_graph import DisclosureGraphService
from app.vector.registry import CompanyVectorStoreRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_settings() -> Settings:
    """실행 위치와 무관하게 저장소 루트의 .env를 읽는다(MCP 서버는 CLI의 임시 폴더에서 뜬다)."""
    return Settings(_env_file=REPO_ROOT / ".env")


def open_tool_context(settings: Settings, corp_code: str) -> tuple[ToolContext, Callable[[], None]]:
    """ToolContext와, 만든 연결을 모두 닫는 함수를 돌려준다."""
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password)
    )
    ollama = OllamaClient(settings)
    dart = DartApiClient(settings)
    tavily = TavilyClient(settings)
    graph = DisclosureGraphService(driver)
    registry = CompanyVectorStoreRegistry(driver, ollama, settings)
    chat = CompanyChatService(ollama, ClaudeCliClient(settings), registry, graph)

    def close() -> None:
        tavily.close()
        dart.close()
        ollama.close()
        driver.close()

    return ToolContext(corp_code, chat, graph, dart, tavily, driver), close
