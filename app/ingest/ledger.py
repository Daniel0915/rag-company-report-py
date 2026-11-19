"""색인 기록 파일(`접수번호/파일명` -> 해시·청크 ID) 읽기/쓰기."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class LedgerError(RuntimeError):
    pass


@dataclass
class LedgerRecord:
    hash: str
    chunkIds: list[str]


def _read_records(path: Path) -> dict[str, LedgerRecord]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise LedgerError(f"기록 파일을 읽을 수 없습니다: {path}") from e
    if not isinstance(raw, dict):
        raise LedgerError(f"기록 파일 형식이 다릅니다: {path}")
    records: dict[str, LedgerRecord] = {}
    for key, value in raw.items():
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("hash"), str)
            or not isinstance(value.get("chunkIds"), list)
            or not all(isinstance(c, str) for c in value["chunkIds"])
        ):
            raise LedgerError(f"기록 파일 형식이 다릅니다: {path} ({key})")
        records[key] = LedgerRecord(value["hash"], list(value["chunkIds"]))
    return records


class IndexLedger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records: dict[str, LedgerRecord] = {}
        self._rcept_nos: set[str] = set()
        self.reload()

    def reload(self) -> None:
        self._records = _read_records(self._path)
        self._rcept_nos = {key.split("/", 1)[0] for key in self._records}

    def has_rcept(self, rcept_no: str) -> bool:
        return rcept_no in self._rcept_nos

    def get(self, key: str) -> LedgerRecord | None:
        return self._records.get(key)

    def put(self, key: str, hash: str, chunk_ids: list[str]) -> None:
        self._records[key] = LedgerRecord(hash, list(chunk_ids))
        self._rcept_nos.add(key.split("/", 1)[0])

    def mark_processed_without_files(self, rcept_no: str) -> None:
        self.put(f"{rcept_no}/-", "", [])

    def save(self) -> None:
        payload = {
            key: {"hash": r.hash, "chunkIds": r.chunkIds}
            for key, r in self._records.items()
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            raise LedgerError(f"기록 파일 저장 실패: {self._path}") from e

    def rcept_nos(self) -> set[str]:
        return set(self._rcept_nos)

    def is_empty(self) -> bool:
        return not self._records

    def import_from(self, source: str | Path) -> int:
        if not self.is_empty():
            raise LedgerError("기록이 비어 있지 않아 가져올 수 없습니다")
        self._records = _read_records(Path(source))
        self._rcept_nos = {key.split("/", 1)[0] for key in self._records}
        self.save()
        return len(self._records)
