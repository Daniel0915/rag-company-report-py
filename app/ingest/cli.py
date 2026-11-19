"""`python -m app.ingest` 명령줄: 색인 기록 가져오기, 과거 수집, 주간 갱신, 상태 조회."""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date
from pathlib import Path

from neo4j import GraphDatabase

from app import watchlist
from app.config import Settings, get_settings
from app.dependencies import Container
from app.ingest.guard import count_disclosure_rcept_nos
from app.ingest.ledger import IndexLedger, LedgerError
from app.ingest.lock import IngestBusyError
from app.ingest.notify import (
    ErrorCollector,
    RedactingFormatter,
    build_notification,
    redact,
    send_notification,
    show_failure_alert,
)
from app.ingest.preflight import run_preflight
from app.ingest.runner import RunSummary
from app.ingest.state import load_state
from app.services.company_report_index import ALL_DISCLOSURE_TYPES

logger = logging.getLogger("app.ingest")

FAILURE_TITLE = "공시 수집 실패"
_LOG_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}(\.errors)?\.log$")


def prune_logs(log_dir: Path, today: date, keep_days: int = 30) -> int:
    """`YYYY-MM-DD.log`와 `YYYY-MM-DD.errors.log` 중 today - keep_days 보다 오래된 파일을 지우고 지운 수를 돌려준다."""
    if not log_dir.is_dir():
        return 0
    removed = 0
    for path in log_dir.iterdir():
        if not _LOG_NAME.match(path.name):
            continue
        try:
            log_date = date.fromisoformat(path.name[:10])
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


def _setup_logging(log_dir: Path, today: date) -> ErrorCollector:
    log_dir.mkdir(parents=True, exist_ok=True)
    # 모든 핸들러가 같은 포매터를 써서 로그 파일·콘솔 어디에도 API 키가 남지 않게 한다.
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_dir / f"{today.isoformat()}.log", mode="a", encoding="utf-8")
    # 수천 줄 로그에 묻히지 않도록 WARNING 이상만 따로 모은다.
    errors_handler = logging.FileHandler(errors_log_path(log_dir, today), mode="a", encoding="utf-8")
    errors_handler.setLevel(logging.WARNING)
    console = logging.StreamHandler()
    collector = ErrorCollector()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # httpx는 요청 URL(DART API 키 포함)을 INFO로 남기고 호출마다 한 줄씩 로그를 채운다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    for handler in (file_handler, errors_handler, console):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.addHandler(collector)
    return collector


def errors_log_path(log_dir: Path, today: date) -> Path:
    return log_dir / f"{today.isoformat()}.errors.log"


def _alert_failure(title: str, message: str, collector: ErrorCollector, errors_log: Path) -> None:
    """한 줄 배너를 보낸 뒤 오류 내용이 담긴 알림창을 띄운다."""
    message = redact(message)  # 어떤 경로의 문장이든 API 키가 알림에 남지 않게 한다
    send_notification(title, message)
    show_failure_alert(title, message, collector.lines, errors_log)


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


def _advances_success_date(args: argparse.Namespace) -> bool:
    """기본 범위(옵션 없는 backfill) 전체 수집만 마지막 성공일을 앞당긴다."""
    return not (args.since or args.corp or args.types)


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    today = date.today()
    log_dir = Path(settings.ingest_log_dir)
    collector = _setup_logging(log_dir, today)
    errors_log = errors_log_path(log_dir, today)
    prune_logs(log_dir, today)

    container: Container | None = None
    try:
        container = Container.build()
        problems = run_preflight(container.driver, container.ollama, container.dart, container.settings)
        if problems:
            for problem in problems:
                logger.error("실행 전 점검 실패: %s", problem)
            _alert_failure(FAILURE_TITLE, problems[0], collector, errors_log)
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
                advance_success_date=_advances_success_date(args),
            )
        return _finish(summary, collector, errors_log)
    except IngestBusyError:
        logger.info("이미 수집이 실행 중이라 건너뜁니다")
        return 0
    except Exception as e:
        logger.exception("수집 중 예상 못 한 오류")
        _alert_failure(FAILURE_TITLE, f"{type(e).__name__}: {e}", collector, errors_log)
        return 1
    finally:
        if container is not None:
            container.close()


def _finish(summary: RunSummary, collector: ErrorCollector, errors_log: Path) -> int:
    notification = build_notification(summary)
    if notification is not None:
        title, message = notification
        _alert_failure(title, _first_sentence(message), collector, errors_log)
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
