"""평가 정답셋·후보 파일(YAML) 형식, 읽기·쓰기·검증, 후보 승인(accept)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

from app.agent.external_mcp import EXTERNAL_TOOL_NAMES
from app.agent.tools import TOOL_NAMES

ORIGINS = ("generated", "manual")
REVIEWS = ("pending", "ok", "drop")
_GENERATED_ID = re.compile(r"^q(\d+)$")
# 외부 MCP 도구는 설정과 상관없이 정답셋에 적을 수 있다(꺼진 상태 실행이 기준선이 된다).
KNOWN_TOOL_NAMES: tuple[str, ...] = TOOL_NAMES + EXTERNAL_TOOL_NAMES


class DatasetError(ValueError):
    """정답셋·후보 파일이 형식에 맞지 않을 때."""


@dataclass(frozen=True)
class Section:
    rcept_no: str
    source_file: str
    section_order: int
    section_title: str

    def __post_init__(self) -> None:
        # YAML에서 따옴표로 감싼 숫자("12")도 정수로 비교되게 맞춘다. 정수로 못 바꾸면 ValueError.
        object.__setattr__(self, "section_order", int(self.section_order))

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
    expected_tools: list[str] = field(default_factory=list)  # 이 중 하나라도 쓰면 된다. 에이전트 평가에서만 쓴다


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
    for name in ("must_include", "expected_sections", "expected_tools"):
        if entry.get(name) is not None and not isinstance(entry[name], list):
            raise DatasetError(f"{item_id}: {name}는 목록이어야 합니다 ({entry[name]!r})")
    tools = entry.get("expected_tools") or []
    if not all(isinstance(t, str) and t in KNOWN_TOOL_NAMES for t in tools):
        raise DatasetError(f"{item_id}: expected_tools는 {list(KNOWN_TOOL_NAMES)} 중에서 골라야 합니다 ({tools!r})")
    try:
        kwargs = dict(entry)
        kwargs["expected_sections"] = [Section(**s) for s in entry.get("expected_sections") or []]
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
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


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """여러 줄 문자열은 사람이 읽기 좋게 블록(|) 스타일로 쓴다. 줄 끝 공백이 있으면 PyYAML이 블록을 못 써서 먼저 뗀다."""
    if "\n" in data:
        data = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _BlockDumper(yaml.SafeDumper):
    pass


_BlockDumper.add_representer(str, _represent_str)


def _dump(path: str | Path, items: list[Item]) -> None:
    entries = [{k: v for k, v in asdict(item).items() if v is not None and v != []} for item in items]
    text = yaml.dump(entries, Dumper=_BlockDumper, allow_unicode=True, sort_keys=False)
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
