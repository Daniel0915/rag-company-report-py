"""
기업(corp_code)마다 별도의 Neo4j 벡터 인덱스/라벨을 쓰는 VectorStore를 만들고 캐싱한다.
모든 기업이 라벨 하나를 공유하면 벡터 인덱스가 corp_code 필터를 ANN 검색 이후에
적용(post-filter)하는 탓에 topK가 작을 때 다른 기업 청크만 뽑히고 필터 후 0건이 되는
문제가 있다. 기업별로 인덱스 자체를 분리하면 다른 기업 데이터가 애초에 후보에 안 섞인다.

워치리스트가 코드에 하드코딩된 소규모 고정 목록이라, 지연 생성 대신 시작 시점에 전부 만든다.
"""
from __future__ import annotations

from neo4j import Driver

from app.config import Settings
from app.llm.ollama import OllamaClient
from app.vector.neo4j_store import Neo4jVectorStore
from app.watchlist import WATCHLIST


def label_for(corp_code: str) -> str:
    return f"CompanyReportChunk_{corp_code}"


class CompanyVectorStoreRegistry:
    def __init__(self, driver: Driver, embedding_client: OllamaClient, settings: Settings) -> None:
        self._driver = driver
        self._embedding = embedding_client
        self._settings = settings
        self._stores: dict[str, Neo4jVectorStore] = {}

    def init_watchlist(self) -> None:
        for company in WATCHLIST:
            self.for_company(company.corp_code)

    def for_company(self, corp_code: str) -> Neo4jVectorStore:
        store = self._stores.get(corp_code)
        if store is None:
            store = self._create_store(corp_code)
            self._stores[corp_code] = store
        return store

    def _create_store(self, corp_code: str) -> Neo4jVectorStore:
        label = label_for(corp_code)
        store = Neo4jVectorStore(
            driver=self._driver,
            embedding_client=self._embedding,
            label=label,
            index_name=f"company-report-index-{corp_code}",
            embedding_dimension=self._settings.embedding_dimension,
            constraint_name=f"{label}_unique_idx",
        )
        store.initialize_schema()
        return store
