from dataclasses import dataclass, field
from typing import Any


@dataclass
class DisclosureItem:
    corp_code: str = ""
    corp_name: str = ""
    stock_code: str = ""
    report_nm: str = ""
    rcept_no: str = ""
    flr_nm: str = ""
    rcept_dt: str = ""
    rm: str = ""

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "DisclosureItem":
        return cls(**{f: raw.get(f, "") or "" for f in cls.__dataclass_fields__})


@dataclass
class DartListResponse:
    status: str = ""
    message: str = ""
    page_no: int | None = None
    page_count: int | None = None
    total_count: int | None = None
    total_page: int | None = None
    items: list[DisclosureItem] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return self.status == "000"

    @property
    def no_data(self) -> bool:
        return self.status == "013"

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "DartListResponse":
        return cls(
            status=raw.get("status", ""),
            message=raw.get("message", ""),
            page_no=raw.get("page_no"),
            page_count=raw.get("page_count"),
            total_count=raw.get("total_count"),
            total_page=raw.get("total_page"),
            items=[DisclosureItem.from_json(x) for x in (raw.get("list") or [])],
        )


@dataclass
class CorpCode:
    corp_code: str
    corp_name: str | None
    corp_eng_name: str | None
    stock_code: str | None
    modify_date: str | None
