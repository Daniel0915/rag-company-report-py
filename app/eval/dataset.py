"""평가 정답셋·후보 파일(YAML) 형식, 읽기·쓰기·검증, 후보 승인(accept)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

ORIGINS = ("generated", "manual")
REVIEWS = ("pending", "ok", "drop")
_GENERATED_ID = re.compile(r"^q(\d+)$")


class DatasetError(ValueError):
    """정답셋·후보 파일이 형식에 맞지 않을 때."""


@dataclass(frozen=True)
class Section:
    rcept_no: str
    source_file: str
    section_order: int
    section_title: str

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.rcept_no, self.source_file, self.section_order)


@dataclass
class Item:
    id: str
    corp_code: str
    question: str
    answerable: bool
    origin: str
    expected_answer: str | None = None
    must_include: list[str] = field(default_factory=list)
    expected_sections: list[Section] = field(default_factory=list)
    pblntf_ty: str | None = None


@dataclass
class Candidate(Item):
    review: str = "pending"
    source_chunk_text: str = ""


def _read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    if not isinstance(data, list):
        raise DatasetError(f"{path}: 최상위는 목록이어야 합니다")
    return data


def _parse(entry: object, cls: type[Item]) -> Item:
    if not isinstance(entry, dict):
        raise DatasetError(f"문항은 키-값 목록이어야 합니다 ({entry!r})")
    item_id = entry.get("id")
    if not isinstance(entry.get("corp_code"), str):
        raise DatasetError(f"{item_id}: corp_code를 따옴표로 감싸세요 (숫자로 읽히면 앞자리 0이 사라집니다)")
    unknown = set(entry) - {f.name for f in fields(cls)}
    if unknown:
        raise DatasetError(f"{item_id}: 알 수 없는 필드 {sorted(unknown)}")
    try:
        kwargs = dict(entry)
        kwargs["expected_sections"] = [Section(**s) for s in entry.get("expected_sections") or []]
        return cls(**kwargs)
    except TypeError as exc:
        raise DatasetError(f"{item_id}: 필드가 올바르지 않습니다 ({exc})") from exc


def _validate(items: list[Item]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise DatasetError(f"{item.id}: id가 중복됩니다")
        seen.add(item.id)
        if item.origin not in ORIGINS:
            raise DatasetError(f"{item.id}: origin은 {'|'.join(ORIGINS)} 중 하나여야 합니다 ({item.origin!r})")
        if item.answerable and not (item.must_include and item.expected_sections):
            raise DatasetError(f"{item.id}: answerable 문항은 must_include와 expected_sections가 필요합니다")
        review = getattr(item, "review", "pending")
        if review not in REVIEWS:
            raise DatasetError(f"{item.id}: review는 {'|'.join(REVIEWS)} 중 하나여야 합니다 ({review!r})")


def _load(path: str | Path, cls: type[Item]) -> list[Item]:
    items = [_parse(entry, cls) for entry in _read_entries(Path(path))]
    _validate(items)
    return items


def load_items(path: str | Path) -> list[Item]:
    return _load(path, Item)


def load_candidates(path: str | Path) -> list[Candidate]:
    return _load(path, Candidate)  # type: ignore[return-value]


def _dump(path: str | Path, items: list[Item]) -> None:
    entries = [{k: v for k, v in asdict(item).items() if v is not None and v != []} for item in items]
    text = yaml.safe_dump(entries, allow_unicode=True, sort_keys=False)
    Path(path).write_text(text, encoding="utf-8")


def save_items(path: str | Path, items: list[Item]) -> None:
    _dump(path, items)


def save_candidates(path: str | Path, candidates: list[Candidate]) -> None:
    _dump(path, candidates)


def _next_number(items: list[Item]) -> int:
    numbers = [int(m.group(1)) for i in items if (m := _GENERATED_ID.match(i.id))]
    return max(numbers, default=0) + 1


def accept(candidates_path: str | Path, dataset_path: str | Path) -> tuple[int, int]:
    """review가 ok인 후보를 정답셋으로 옮기고 drop은 버린다. (옮긴 수, 버린 수)를 돌려준다."""
    candidates = load_candidates(candidates_path)
    dataset = load_items(dataset_path)
    number = _next_number(dataset)
    moved = dropped = 0
    remaining: list[Candidate] = []
    for cand in candidates:
        if cand.review == "ok":
            data = {f.name: getattr(cand, f.name) for f in fields(Item)}
            data["id"] = f"q{number:04d}"
            dataset.append(Item(**data))
            number += 1
            moved += 1
        elif cand.review == "drop":
            dropped += 1
        else:
            remaining.append(cand)
    save_items(dataset_path, dataset)
    save_candidates(candidates_path, remaining)
    return moved, dropped


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def existing_section_keys(*paths: str | Path) -> set[tuple[str, str, int]]:
    """정답셋·후보 파일들에 들어 있는 모든 기대 섹션 키."""
    keys: set[tuple[str, str, int]] = set()
    for path in paths:
        for item in _load(path, Candidate):
            keys.update(s.key for s in item.expected_sections)
    return keys
