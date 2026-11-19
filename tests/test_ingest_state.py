from dataclasses import replace
from datetime import date

from app.ingest.state import (
    IngestState,
    PendingFailure,
    load_state,
    record_failure,
    record_success,
    save_state,
    update_window,
)


def test_window_overlaps_seven_days_from_last_success():
    s = IngestState(last_success_date=date(2026, 11, 14))
    assert update_window(s, date(2026, 11, 21)) == (date(2026, 11, 7), date(2026, 11, 21))


def test_window_without_history_is_two_years():
    assert update_window(IngestState(), date(2026, 11, 21)) == (date(2024, 11, 21), date(2026, 11, 21))


def test_window_two_years_from_leap_day_uses_feb_28():
    assert update_window(IngestState(), date(2028, 2, 29)) == (date(2026, 2, 28), date(2028, 2, 29))


def test_fifth_failure_moves_to_given_up():
    s = IngestState()
    f = PendingFailure("1", "00126380", "I", "영업(잠정)실적(공정공시)", "20261030", "timeout", 0)
    for _ in range(5):
        record_failure(s, replace(f))
    assert s.pending == [] and s.given_up[0].attempts == 5


def test_success_removes_pending():
    s = IngestState()
    record_failure(s, PendingFailure("1", "c", "A", "r", "d", "e", 0))
    record_success(s, "1")
    assert s.pending == []


def test_state_round_trip(tmp_path):
    p = tmp_path / "s.json"
    s = IngestState(last_run_at="2026-11-21T07:10:00", last_success_date=date(2026, 11, 21))
    save_state(p, s)
    assert load_state(p) == s


def test_save_state_is_atomic_and_leaves_no_tmp_file(tmp_path, monkeypatch):
    import os

    path = tmp_path / "state.json"
    save_state(path, IngestState(last_run_at="a"))
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (replaced.append((str(s), str(d))), real(s, d))[1])
    save_state(path, IngestState(last_run_at="b"))
    assert replaced == [(str(path) + ".tmp", str(path))]
    assert load_state(path).last_run_at == "b"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
