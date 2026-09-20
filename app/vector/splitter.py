"""
Spring AI TokenTextSplitter와 같은 방식으로 문서를 청킹한다(cl100k_base 토크나이저,
기본 800토큰). 같은 Neo4j 인덱스를 Java 구현과 공유해도 청크 크기가 어긋나지 않도록
경계 규칙(마지막 문장부호에서 자르기)까지 그대로 옮겼다.
"""
from __future__ import annotations

from functools import lru_cache

import tiktoken

from app.vector.document import Document


@lru_cache
def _encoding() -> "tiktoken.Encoding":
    return tiktoken.get_encoding("cl100k_base")


class TokenTextSplitter:
    def __init__(
        self,
        chunk_size: int = 800,
        min_chunk_size_chars: int = 350,
        min_chunk_length_to_embed: int = 5,
        max_num_chunks: int = 10000,
        keep_separator: bool = True,
    ) -> None:
        self.chunk_size = chunk_size
        self.min_chunk_size_chars = min_chunk_size_chars
        self.min_chunk_length_to_embed = min_chunk_length_to_embed
        self.max_num_chunks = max_num_chunks
        self.keep_separator = keep_separator

    def apply(self, documents: list[Document]) -> list[Document]:
        """각 문서를 청크로 쪼개되 메타데이터는 그대로 물려준다."""
        chunks: list[Document] = []
        for document in documents:
            for text in self.split(document.text):
                chunks.append(Document(text=text, metadata=dict(document.metadata)))
        return chunks

    def split(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []

        encoding = _encoding()
        tokens = encoding.encode(text, disallowed_special=())
        chunks: list[str] = []

        while tokens and len(chunks) < self.max_num_chunks:
            chunk_tokens = tokens[: self.chunk_size]
            chunk_text = encoding.decode(chunk_tokens)
            if not chunk_text.strip():
                tokens = tokens[len(chunk_tokens) :]
                continue

            # 청크 끝이 문장 중간에서 잘리지 않도록, 충분히 뒤쪽에 있는 마지막 문장부호에서 자른다.
            last_punctuation = max(
                chunk_text.rfind("."),
                chunk_text.rfind("?"),
                chunk_text.rfind("!"),
                chunk_text.rfind("\n"),
            )
            if last_punctuation != -1 and last_punctuation > self.min_chunk_size_chars:
                chunk_text = chunk_text[: last_punctuation + 1]

            trimmed = chunk_text if self.keep_separator else chunk_text.replace("\n", " ")
            trimmed = trimmed.strip()
            if len(trimmed) > self.min_chunk_length_to_embed:
                chunks.append(trimmed)

            tokens = tokens[len(encoding.encode(chunk_text, disallowed_special=())) :]

        if tokens:
            remaining = encoding.decode(tokens).strip()
            if len(remaining) > self.min_chunk_length_to_embed:
                chunks.append(remaining)

        return chunks
