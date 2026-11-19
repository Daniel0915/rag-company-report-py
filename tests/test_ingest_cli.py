from datetime import date

import pytest

from app.ingest.cli import build_parser, ledger_guard, main, prune_logs
from app.ingest.ledger import IndexLedger


def test_ledger_guard_refuses_empty_ledger_with_existing_chunks(tmp_path):
    assert "import-ledger" in ledger_guard(IndexLedger(tmp_path / "l.json"), 28362)


def test_ledger_guard_allows_fresh_install(tmp_path):
    assert ledger_guard(IndexLedger(tmp_path / "l.json"), 0) is None


def test_prune_logs_keeps_last_30_days(tmp_path):
    for name in ["2026-10-20.log", "2026-10-22.log", "notes.txt"]:
        (tmp_path / name).write_text("")
    assert prune_logs(tmp_path, date(2026, 11, 21)) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-22.log", "notes.txt"]


def test_parse_backfill_args():
    ns = build_parser().parse_args(
        ["backfill", "--since", "2025-01-01", "--corp", "00126380", "--types", "A,I"]
    )
    assert ns.since == date(2025, 1, 1) and ns.corp == ["00126380"] and ns.types == ["A", "I"]


@pytest.mark.parametrize("extra", [["--corp", "99999999"], ["--types", "A,Z"]])
def test_backfill_rejects_unknown_values_before_building_container(extra, monkeypatch, capsys):
    def boom():
        raise AssertionError("컨테이너를 만들면 안 된다")

    monkeypatch.setattr("app.ingest.cli.Container.build", boom)
    assert main(["backfill", *extra]) == 1
    assert capsys.readouterr().out.strip()


def test_setup_logging_keeps_http_request_urls_out_of_log_file(tmp_path):
    import logging

    from app.ingest.cli import _setup_logging

    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    httpx_level = logging.getLogger("httpx").level
    httpcore_level = logging.getLogger("httpcore").level
    try:
        _setup_logging(tmp_path, date(2026, 11, 21))
        logging.getLogger("httpx").info("HTTP Request: GET https://x/list.json?crtfc_key=SECRET")
        logging.getLogger("app.ingest").info("hello")
        for h in root.handlers:
            h.flush()
    finally:
        for h in list(root.handlers):
            if h not in before_handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(before_level)
        logging.getLogger("httpx").setLevel(httpx_level)
        logging.getLogger("httpcore").setLevel(httpcore_level)
    text = (tmp_path / "2026-11-21.log").read_text(encoding="utf-8")
    assert "hello" in text and "SECRET" not in text
