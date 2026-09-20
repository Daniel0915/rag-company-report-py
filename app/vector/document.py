"""Spring AI Document 대응. 벡터스토어에 넣는 텍스트 + 메타데이터 한 조각."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Document:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    score: float | None = None

    def with_metadata(self, extra: dict[str, Any]) -> "Document":
        merged = dict(self.metadata)
        merged.update(extra)
        return Document(text=self.text, metadata=merged)
