"""
앱 전역 싱글턴(드라이버/클라이언트/서비스)을 한 곳에서 조립한다. Spring의 컴포넌트
스캔 대신, lifespan에서 build()로 한 번 만들고 FastAPI 라우터는 Depends로 꺼내 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from neo4j import Driver, GraphDatabase

from app.agent.service import AgentService
from app.config import Settings, get_settings
from app.dart.client import DartApiClient
from app.ingest.guard import count_disclosure_chunks
from app.ingest.ledger import IndexLedger
from app.ingest.runner import IngestRunner
from app.llm.claude_cli import ClaudeCliClient
from app.llm.ollama import OllamaClient
from app.llm.openai_compat import OpenAICompatClient
from app.naver.article_scraper import NaverArticleScraper
from app.naver.search_scraper import NaverSearchScraper
from app.services.admin_document import AdminDocumentService
from app.services.company_chat import CompanyChatService
from app.services.company_report_index import CompanyReportIndexService
from app.services.disclosure_graph import DisclosureGraphService
from app.services.indexed_chunk import IndexedChunkService
from app.services.news_graph import NewsGraphService
from app.services.news_ingest import NewsIngestService
from app.vector.registry import CompanyVectorStoreRegistry


@dataclass
class Container:
    settings: Settings
    driver: Driver
    ollama: OllamaClient
    claude: ClaudeCliClient
    deepseek: OpenAICompatClient
    dart: DartApiClient
    search_scraper: NaverSearchScraper
    article_scraper: NaverArticleScraper
    registry: CompanyVectorStoreRegistry
    disclosure_graph: DisclosureGraphService
    news_graph: NewsGraphService
    index_service: CompanyReportIndexService
    ingest_runner: IngestRunner
    news_service: NewsIngestService
    chunk_service: IndexedChunkService
    admin_service: AdminDocumentService
    chat_service: CompanyChatService
    agent_ollama: OllamaClient
    agent_service: AgentService

    @classmethod
    def build(cls) -> "Container":
        settings = get_settings()
        driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password)
        )
        ollama = OllamaClient(settings)
        claude = ClaudeCliClient(settings)
        deepseek = OpenAICompatClient(settings)
        agent_ollama = OllamaClient(settings, options={"num_ctx": settings.agent_ollama_num_ctx})
        dart = DartApiClient(settings)
        search_scraper = NaverSearchScraper()
        article_scraper = NaverArticleScraper()

        registry = CompanyVectorStoreRegistry(driver, ollama, settings)
        ledger = IndexLedger(settings.company_report_index_file)
        registry.init_watchlist()

        disclosure_graph = DisclosureGraphService(driver)
        news_graph = NewsGraphService(driver)
        index_service = CompanyReportIndexService(dart, registry, disclosure_graph, ledger)

        chat_service = CompanyChatService(ollama, claude, registry, disclosure_graph, deepseek)

        return cls(
            settings=settings,
            driver=driver,
            ollama=ollama,
            claude=claude,
            deepseek=deepseek,
            dart=dart,
            search_scraper=search_scraper,
            article_scraper=article_scraper,
            registry=registry,
            disclosure_graph=disclosure_graph,
            news_graph=news_graph,
            index_service=index_service,
            ingest_runner=IngestRunner(
                index_service, ledger, settings, lambda: count_disclosure_chunks(driver)
            ),
            news_service=NewsIngestService(
                search_scraper,
                article_scraper,
                registry,
                news_graph,
                settings.news_index_file,
                settings.news_articles_per_company,
            ),
            chunk_service=IndexedChunkService(driver),
            admin_service=AdminDocumentService(
                registry,
                settings.admin_document_registry_file,
                settings.admin_document_upload_dir,
            ),
            chat_service=chat_service,
            agent_ollama=agent_ollama,
            agent_service=AgentService(settings, chat_service, agent_ollama),
        )

    def close(self) -> None:
        self.ollama.close()
        self.agent_ollama.close()
        self.deepseek.close()
        self.dart.close()
        self.search_scraper.close()
        self.article_scraper.close()
        self.driver.close()


def get_container(request: Request) -> Container:
    return request.app.state.container
