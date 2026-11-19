"""`python -m app.ingest` 명령줄: 색인 기록 가져오기, 과거 수집, 주간 갱신, 상태 조회."""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date
from pathlib import Path

from neo4j import Driver, GraphDatabase

from app import watchlist
from app.config import Settings, get_settings
from app.dependencies import Container
from app.ingest.ledger import IndexLedger, LedgerError
from app.ingest.lock import IngestBusyError
from app.ingest.notify import build_notification, send_notification
from app.ingest.preflight import run_preflight
from app.ingest.runner import RunSummary
from app.ingest.state import load_state
from app.services.company_report_index import ALL_DISCLOSURE_TYPES

logger = logging.getLogger("app.ingest")

FAILURE_TITLE = "공시 수집 실패"
_LOG_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.log$")

_DISCLOSURE_NODES = (
    "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'CompanyReportChunk_') "
    "AND n.`metadata.doc_type` IS NULL"
)


def ledger_guard(ledger: IndexLedger, disclosure_chunk_count: int) -> str | None:
    """기록이 비었는데 Neo4j에 공시 청크가 있으면 중복 색인을 막기 위해 거부 메시지를 돌려준다."""
    if ledger.is_empty() and disclosure_chunk_count > 0:
        return (
            "기록 파일이 비어 있는데 Neo4j에 공시 청크가 있습니다. "
            "먼저 python -m app.ingest import-ledger <Spring 기록 파일>을 실행하세요"
        )
    return None


def count_disclosure_chunks(driver: Driver) -> int:
    with driver.session() as session:
        return session.run(f"{_DISCLOSURE_NODES} RETURN count(n) AS total").single()["total"]


def count_disclosure_rcept_nos(driver: Driver) -> int:
    query = f"{_DISCLOSURE_NODES} RETURN count(DISTINCT n.`metadata.rcept_no`) AS total"
    with driver.session() as session:
        return session.run(query).single()["total"]


def prune_logs(log_dir: Path, today: date, keep_days: int = 30) -> int:
    """`YYYY-MM-DD.log` 중 today - keep_days 보다 오래된 파일을 지우고 지운 수를 돌려준다."""
    if not log_dir.is_dir():
        return 0
    removed = 0
    for path in log_dir.iterdir():
        if not _LOG_NAME.match(path.name):
            continue
        try:
            log_date = date.fromisoformat(path.name[:-4])
        except ValueError:
            continue
        if (today - log_date).days > keep_days:
            path.unlink()
            removed += 1
    return removed


def _two_years_ago(today: date) -> date:
    try:
        return today.replace(year=today.year - 2)
    except ValueError:  # 2월 29일
        return today.replace(year=today.year - 2, day=28)


def _split_types(value: str) -> list[str]:
    return [t.strip() for t in value.split(",") if t.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ingest", description="DART 공시 수집")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-ledger", help="Spring 기록 파일을 가져온다")
    p.add_argument("path", type=Path)

    p = sub.add_parser("backfill", help="기간을 지정해 과거 공시를 수집한다")
    p.add_argument("--since", type=date.fromisoformat, help="시작일 YYYY-MM-DD (기본: 2년 전)")
    p.add_argument("--corp", nargs="+", help="기업 고유번호 (기본: 관심 기업 전체)")
    p.add_argument("--types", type=_split_types, help="공시 유형 쉼표 구분 (기본: A~J 전부)")

    sub.add_parser("update", help="마지막 성공일 7일 전부터 오늘까지 갱신한다")
    sub.add_parser("status", help="마지막 실행 상태를 보여 준다")
    return parser


def _setup_logging(log_dir: Path, today: date) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_dir / f"{today.isoformat()}.log", mode="a", encoding="utf-8")
    console = logging.StreamHandler()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # httpx는 요청 URL(DART API 키 포함)을 INFO로 남기고 호출마다 한 줄씩 로그를 채운다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def _notify(title: str, message: str) -> None:
    send_notification(title, message)


def _first_sentence(text: str) -> str:
    return text.split(". ", 1)[0]


def _cmd_import_ledger(args: argparse.Namespace, settings: Settings) -> int:
    ledger = IndexLedger(settings.company_report_index_file)
    try:
        count = ledger.import_from(args.path)
    except (LedgerError, OSError) as e:
        print(f"가져오기 실패: {e}")
        return 1
    print(f"기록 {count}건을 가져왔습니다")
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password)
    )
    try:
        neo4j_count = count_disclosure_rcept_nos(driver)
        print(f"기록 파일 접수번호 {len(ledger.rcept_nos())}개 / Neo4j 공시 청크 접수번호 {neo4j_count}개")
    except Exception as e:
        print(f"Neo4j에 연결할 수 없어 접수번호 비교를 건너뜁니다: {e}")
    finally:
        driver.close()
    return 0


def _cmd_status(settings: Settings) -> int:
    state = load_state(settings.ingest_state_file)
    print(f"마지막 실행 시각: {state.last_run_at or '없음'}")
    print(f"마지막 성공일: {state.last_success_date.isoformat() if state.last_success_date else '없음'}")
    print(f"재시도 대기: {len(state.pending)}건")
    print(f"재시도 중단: {len(state.given_up)}건")
    for p in state.given_up:
        print(f"  {p.rcept_no} {p.report_nm} - {p.error}")
    return 0


def _validate_backfill(args: argparse.Namespace) -> str | None:
    if args.corp:
        unknown = [c for c in args.corp if watchlist.find(c) is None]
        if unknown:
            return f"관심 기업 목록에 없는 기업 코드입니다: {', '.join(unknown)}"
    if args.types:
        unknown = [t for t in args.types if t not in ALL_DISCLOSURE_TYPES]
        if unknown:
            return (
                f"알 수 없는 공시 유형입니다: {', '.join(unknown)} "
                f"(가능: {','.join(ALL_DISCLOSURE_TYPES)})"
            )
    return None


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    today = date.today()
    log_dir = Path(settings.ingest_log_dir)
    _setup_logging(log_dir, today)
    prune_logs(log_dir, today)

    container: Container | None = None
    try:
        container = Container.build()
        problems = run_preflight(container.driver, container.ollama, container.dart, container.settings)
        if problems:
            for problem in problems:
                logger.error("실행 전 점검 실패: %s", problem)
            _notify(FAILURE_TITLE, problems[0])
            return 1

        ledger = container.ingest_runner._ledger  # 색인 서비스와 같은 기록 인스턴스를 쓴다
        refusal = ledger_guard(ledger, count_disclosure_chunks(container.driver))
        if refusal:
            logger.error(refusal)
            _notify(FAILURE_TITLE, _first_sentence(refusal))
            return 1

        runner = container.ingest_runner
        if args.command == "update":
            summary = runner.run_update()
        else:
            summary = runner.run(
                args.since or _two_years_ago(today),
                today,
                corp_codes=args.corp,
                types=args.types,
                today=today,
                advance_success_date=False,
            )
        return _finish(summary)
    except IngestBusyError:
        logger.info("이미 수집이 실행 중이라 건너뜁니다")
        return 0
    except Exception as e:
        logger.exception("수집 중 예상 못 한 오류")
        _notify(FAILURE_TITLE, f"{type(e).__name__}: {e}")
        return 1
    finally:
        if container is not None:
            container.close()


def _finish(summary: RunSummary) -> int:
    notification = build_notification(summary)
    if notification is not None:
        _notify(*notification)
    return 1 if summary.aborted_reason else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.command == "import-ledger":
        return _cmd_import_ledger(args, settings)
    if args.command == "status":
        return _cmd_status(settings)
    if args.command == "backfill":
        error = _validate_backfill(args)
        if error:
            print(error)
            return 1
    return _cmd_run(args, settings)
