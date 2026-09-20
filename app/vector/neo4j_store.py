"""
Neo4j 벡터스토어. Spring AI의 Neo4jVectorStore와 같은 노드 스키마(id/text/embedding +
metadata.* 평탄화 속성)와 같은 인덱스/제약조건 이름을 써서, Java 구현이 만든 데이터를
그대로 읽고 쓸 수 있다.
"""
from __future__ import annotations

from typing import Any

from neo4j import Driver

from app.llm.ollama import OllamaClient
from app.vector.document import Document

METADATA_PREFIX = "metadata."


class Neo4jVectorStore:
    def __init__(
        self,
        driver: Driver,
        embedding_client: OllamaClient,
        label: str,
        index_name: str,
        embedding_dimension: int,
        constraint_name: str,
    ) -> None:
        self._driver = driver
        self._embedding = embedding_client
        self._label = label
        self._index_name = index_name
        self._dimension = embedding_dimension
        self._constraint_name = constraint_name

    @property
    def label(self) -> str:
        return self._label

    def initialize_schema(self) -> None:
        with self._driver.session() as session:
            session.run(
                f"CREATE CONSTRAINT {self._constraint_name} IF NOT EXISTS "
                f"FOR (n:`{self._label}`) REQUIRE n.id IS UNIQUE"
            )
            session.run(
                f"CREATE VECTOR INDEX `{self._index_name}` IF NOT EXISTS "
                f"FOR (n:`{self._label}`) ON (n.embedding) "
                "OPTIONS {indexConfig: {"
                "`vector.dimensions`: $dimension, "
                "`vector.similarity_function`: 'cosine'}}",
                dimension=self._dimension,
            )

    def add(self, documents: list[Document], batch_size: int = 16) -> None:
        """문서를 임베딩해서 저장한다. 임베딩 호출은 배치로 묶는다."""
        if not documents:
            return
        query = (
            "UNWIND $rows AS row "
            f"MERGE (n:`{self._label}` {{id: row.id}}) "
            "SET n += row.properties "
            "WITH n, row CALL db.create.setNodeVectorProperty(n, 'embedding', row.embedding) "
            "RETURN count(n)"
        )
        with self._driver.session() as session:
            for start in range(0, len(documents), batch_size):
                batch = documents[start : start + batch_size]
                embeddings = self._embedding.embed([d.text for d in batch])
                rows = [
                    {
                        "id": document.id,
                        "properties": _to_properties(document),
                        "embedding": embedding,
                    }
                    for document, embedding in zip(batch, embeddings)
                ]
                session.run(query, rows=rows)

    def similarity_search(
        self,
        query: str,
        top_k: int = 4,
        similarity_threshold: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[Document]:
        """
        코사인 유사도 상위 top_k개를 반환한다. metadata_filter는 ANN 검색으로 뽑힌 후보에
        대한 WHERE절로 적용되는 post-filter라(Spring AI와 동일), 필터를 걸 때는 호출부가
        top_k를 넉넉히 키워서 불러야 한다.
        """
        embedding = self._embedding.embed_one(query)

        where_clauses = ["score >= $threshold"]
        params: dict[str, Any] = {
            "indexName": self._index_name,
            "topK": top_k,
            "embedding": embedding,
            "threshold": similarity_threshold,
        }
        for i, (key, value) in enumerate((metadata_filter or {}).items()):
            param = f"filter{i}"
            where_clauses.append(f"node.`{METADATA_PREFIX}{key}` = ${param}")
            params[param] = value

        cypher = (
            "CALL db.index.vector.queryNodes($indexName, $topK, $embedding) YIELD node, score "
            f"WHERE {' AND '.join(where_clauses)} "
            "RETURN node, score ORDER BY score DESC"
        )
        with self._driver.session() as session:
            records = list(session.run(cypher, **params))

        return [_to_document(record["node"], record["score"]) for record in records]

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._driver.session() as session:
            session.run(
                f"MATCH (n:`{self._label}`) WHERE n.id IN $ids DETACH DELETE n", ids=ids
            )


def _to_properties(document: Document) -> dict[str, Any]:
    properties: dict[str, Any] = {"text": document.text}
    for key, value in document.metadata.items():
        # Neo4j 속성은 스칼라/스칼라 배열만 되므로 그 외 타입은 문자열로 눕힌다.
        if value is None or isinstance(value, (str, int, float, bool)):
            properties[f"{METADATA_PREFIX}{key}"] = value
        else:
            properties[f"{METADATA_PREFIX}{key}"] = str(value)
    return properties


def _to_document(node: Any, score: float) -> Document:
    metadata = {
        key[len(METADATA_PREFIX) :]: value
        for key, value in node.items()
        if key.startswith(METADATA_PREFIX)
    }
    return Document(
        text=node.get("text", ""),
        metadata=metadata,
        id=node.get("id", ""),
        score=score,
    )
