from datetime import date

import pytest

from app.ingest.cli import _advances_success_date, build_parser, main, prune_logs


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["backfill"], True),
        (["backfill", "--since", "2025-01-01"], False),
        (["backfill", "--corp", "00126380"], False),
        (["backfill", "--types", "A"], False),
    ],
)
def test_only_default_scope_backfill_advances_success_date(argv, expected):
    assert _advances_success_date(build_parser().parse_args(argv)) is expected


def test_prune_logs_keeps_last_30_days(tmp_path):
    names = ["2026-10-20.log", "2026-10-22.log", "2026-10-20.errors.log", "2026-10-22.errors.log", "notes.txt"]
    for name in names:
        (tmp_path / name).write_text("")
    assert prune_logs(tmp_path, date(2026, 11, 21)) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-10-22.errors.log", "2026-10-22.log", "notes.txt"
    ]


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


@pytest.fixture
def restore_logging():
    import logging

    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    httpx_level = logging.getLogger("httpx").level
    httpcore_level = logging.getLogger("httpcore").level
    yield
    for h in list(root.handlers):
        if h not in before_handlers:
            root.removeHandler(h)
            h.close()
    root.setLevel(before_level)
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpcore_level)


def test_errors_log_gets_only_warnings_and_redacted_tracebacks(tmp_path, restore_logging):
    import logging

    from app.ingest.cli import _setup_logging

    _setup_logging(tmp_path, date(2026, 11, 21))
    log = logging.getLogger("app.ingest")
    log.info("ok")
    try:
        raise RuntimeError("GET https://x/list.json?crtfc_key=SECRET&corp_code=1")
    except RuntimeError:
        log.exception("목록 조회 실패")
    for h in logging.getLogger().handlers:
        h.flush()
    errors = (tmp_path / "2026-11-21.errors.log").read_text(encoding="utf-8")
    full = (tmp_path / "2026-11-21.log").read_text(encoding="utf-8")
    assert "Traceback" in errors and "crtfc_key=***" in errors
    assert "ok" not in errors and "SECRET" not in errors
    assert "SECRET" not in full


def _fake_run_env(monkeypatch, tmp_path, summary, restore_logging):
    import logging

    from app.config import Settings

    settings = Settings(ingest_log_dir=str(tmp_path))
    monkeypatch.setattr("app.ingest.cli.get_settings", lambda: settings)

    class FakeRunner:
        def run_update(self):
            if isinstance(summary, Exception):
                raise summary
            if summary.failed:
                logging.getLogger("app.ingest").error("색인 실패 삼성전자 R1: boom")
            return summary

    class FakeContainer:
        driver = ollama = dart = None
        settings = None
        ingest_runner = FakeRunner()

        def close(self):
            pass

    monkeypatch.setattr("app.ingest.cli.Container.build", lambda: FakeContainer())
    monkeypatch.setattr("app.ingest.cli.run_preflight", lambda *a, **k: [])
    banners, alerts = [], []
    monkeypatch.setattr("app.ingest.cli.send_notification", lambda t, m: banners.append((t, m)))
    monkeypatch.setattr("app.ingest.cli.show_failure_alert", lambda *a, **k: alerts.append((a, k)))
    return banners, alerts


def test_partial_failure_run_shows_alert_with_errors(tmp_path, monkeypatch, restore_logging):
    from app.ingest.runner import RunSummary

    banners, alerts = _fake_run_env(monkeypatch, tmp_path, RunSummary(new=1, failed=1), restore_logging)
    assert main(["update"]) == 0
    assert len(banners) == 1 and len(alerts) == 1
    args, kwargs = alerts[0]
    error_lines = kwargs.get("error_lines", args[2] if len(args) > 2 else None)
    assert any("R1: boom" in line for line in error_lines)


def test_successful_run_shows_no_alert(tmp_path, monkeypatch, restore_logging):
    from app.ingest.runner import RunSummary

    banners, alerts = _fake_run_env(monkeypatch, tmp_path, RunSummary(new=1), restore_logging)
    assert main(["update"]) == 0
    assert banners == [] and alerts == []


def test_unexpected_exception_alerts_once_without_api_key(tmp_path, monkeypatch, restore_logging):
    banners, alerts = _fake_run_env(
        monkeypatch, tmp_path, RuntimeError("boom crtfc_key=SECRET"), restore_logging
    )
    assert main(["update"]) == 1
    assert len(banners) == 1 and len(alerts) == 1
    assert banners[0][0] == "공시 수집 실패"
    args, kwargs = alerts[0]
    assert args[0] == "공시 수집 실패"
    assert "SECRET" not in repr(banners) and "SECRET" not in repr(alerts)
    assert str(args[3]).endswith(".errors.log")
