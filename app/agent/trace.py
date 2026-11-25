"""도구 호출 기록(JSON Lines). 한 줄에 호출 하나."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolCallRecord:
    seq: int
    name: str
    arguments: dict
    ok: bool
    result_text: str
    sources: list[dict]


def append_record(path: str | Path, record: ToolCallRecord) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def read_records(path: str | Path) -> list[ToolCallRecord]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [ToolCallRecord(**json.loads(line)) for line in f if line.strip()]
