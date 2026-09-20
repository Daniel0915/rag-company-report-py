"""관심 기업 목록. Java WatchedCompany.ALL 과 동일한 고정 목록."""
from dataclasses import dataclass


@dataclass(frozen=True)
class WatchedCompany:
    name: str
    corp_code: str
    stock_code: str


WATCHLIST: list[WatchedCompany] = [
    WatchedCompany("삼성전자", "00126380", "005930"),
    WatchedCompany("F&F", "01568413", "383220"),
    WatchedCompany("SK하이닉스", "00164779", "000660"),
    WatchedCompany("SK스퀘어", "01596425", "402340"),
    WatchedCompany("현대자동차", "00164742", "005380"),
    WatchedCompany("LG에너지솔루션", "01515323", "373220"),
    WatchedCompany("삼성바이오로직스", "00877059", "207940"),
    WatchedCompany("삼성물산", "00149655", "028260"),
    WatchedCompany("KB금융", "00688996", "105560"),
    WatchedCompany("한화에어로스페이스", "00126566", "012450"),
    WatchedCompany("삼성생명", "00126256", "032830"),
    WatchedCompany("두산에너빌리티", "00159616", "034020"),
    WatchedCompany("신한지주", "00382199", "055550"),
    WatchedCompany("기아", "00106641", "000270"),
    WatchedCompany("HD현대중공업", "01390344", "329180"),
    WatchedCompany("셀트리온", "00413046", "068270"),
    WatchedCompany("현대모비스", "00164788", "012330"),
    WatchedCompany("삼성전기", "00126371", "009150"),
    WatchedCompany("LG전자", "00401731", "066570"),
    WatchedCompany("하나금융지주", "00547583", "086790"),
    WatchedCompany("NAVER", "00266961", "035420"),
    WatchedCompany("엘에스일렉트릭", "00105855", "010120"),
]

BY_CORP_CODE: dict[str, WatchedCompany] = {c.corp_code: c for c in WATCHLIST}


def find(corp_code: str) -> WatchedCompany | None:
    return BY_CORP_CODE.get(corp_code)
