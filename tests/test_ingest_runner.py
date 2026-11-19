from datetime import date

from app.config import Settings
from app.dart.dto import DisclosureItem
from app.dart.errors import DartApiError
from app.ingest.ledger import IndexLedger
from app.ingest.runner import IngestRunner
from app.ingest.state import IngestState, PendingFailure, load_state, save_state
from app.models import IndexResult

SAMSUNG = "00126380"
FNF = "01568413"


class FakeIndex:
    def __init__(self, listings=None, outcomes=None):
        self.listings = listings or {}  # (corp, ty) -> [DisclosureItem]
        self.outcomes = outcomes or {}  # rcept_no -> status str | Exception
        self.list_calls = []
        self.index_calls = []

    def list_disclosures(self, corp_code, bgn_de, end_de, pblntf_ty):
        self.list_calls.append((corp_code, pblntf_ty))
        return self.listings.get((corp_code, pblntf_ty), [])

    def index_disclosure(self, company, item, pblntf_ty):
        self.index_calls.append(item.rcept_no)
        out = self.outcomes.get(item.rcept_no, "new")
        if isinstance(out, Exception):
            raise out
        return IndexResult(
            corpName=company.name, rceptNo=item.rcept_no, reportNm=item.report_nm,
            status=out, chunks=2 if out == "new" else 0,
        )


def item(rcept_no, corp=SAMSUNG):
    return DisclosureItem(corp_code=corp, rcept_no=rcept_no, report_nm="보고서", rcept_dt="20261101")


def make(tmp_path, fake, chunk_count=lambda: 0):
    settings = Settings(
        ingest_state_file=str(tmp_path / "state.json"),
        ingest_lock_file=str(tmp_path / "ingest.lock"),
        company_report_index_file=str(tmp_path / "index.json"),
    )
    runner = IngestRunner(fake, IndexLedger(settings.company_report_index_file), settings, chunk_count)
    return runner, settings


D1, D2 = date(2026, 11, 1), date(2026, 11, 21)


def test_failure_goes_to_pending_and_is_retried_next_run(tmp_path):
    fake = FakeIndex({(SAMSUNG, "A"): [item("R1")]}, {"R1": RuntimeError("boom")})
    runner, s = make(tmp_path, fake)
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert summary.failed == 1
    assert load_state(s.ingest_state_file).pending[0].rcept_no == "R1"

    fake.listings = {}
    fake.outcomes = {}
    fake.index_calls.clear()
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert fake.index_calls == ["R1"]
    assert summary.new == 1 and summary.chunks == 2
    assert load_state(s.ingest_state_file).pending == []


def test_rate_limit_aborts_without_advancing_success_date(tmp_path):
    fake = FakeIndex(
        {(SAMSUNG, "A"): [item("R1"), item("R2"), item("R3")]},
        {"R2": DartApiError("020", "limit")},
    )
    runner, s = make(tmp_path, fake)
    save_state(s.ingest_state_file, IngestState(last_success_date=date(2026, 10, 1)))
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert "R3" not in fake.index_calls
    assert summary.aborted_reason == "DART 하루 호출 한도 초과"
    assert load_state(s.ingest_state_file).last_success_date == date(2026, 10, 1)
    assert [r.rceptNo for r in summary.results] == ["R1"]


def test_same_rcept_no_processed_once_per_run(tmp_path):
    fake = FakeIndex({(SAMSUNG, "A"): [item("R1")], (SAMSUNG, "I"): [item("R1")]})
    runner, s = make(tmp_path, fake)
    save_state(s.ingest_state_file, IngestState(pending=[
        PendingFailure("R1", SAMSUNG, "A", "보고서", "20261101", "x", 1)
    ]))
    runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A", "I"], today=D2)
    assert fake.index_calls == ["R1"]


def test_success_advances_last_success_date(tmp_path):
    runner, s = make(tmp_path, FakeIndex())
    runner.run(D1, D2, today=date(2026, 11, 21))
    assert load_state(s.ingest_state_file).last_success_date == date(2026, 11, 21)


def test_corp_and_type_filters(tmp_path):
    fake = FakeIndex()
    runner, _ = make(tmp_path, fake)
    runner.run(D1, D2, corp_codes=[SAMSUNG], types=["I"], today=D2)
    assert fake.list_calls == [(SAMSUNG, "I")]


def test_given_up_disclosure_is_never_indexed(tmp_path):
    fake = FakeIndex({(SAMSUNG, "A"): [item("R1"), item("R2")]})
    runner, s = make(tmp_path, fake)
    save_state(s.ingest_state_file, IngestState(given_up=[
        PendingFailure("R1", SAMSUNG, "A", "보고서", "20261101", "x", 5)
    ]))
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert fake.index_calls == ["R2"]
    assert summary.failed == 0


def test_failure_reaching_limit_counts_as_given_up(tmp_path):
    fake = FakeIndex({}, {"R1": RuntimeError("boom")})
    runner, s = make(tmp_path, fake)
    save_state(s.ingest_state_file, IngestState(pending=[
        PendingFailure("R1", SAMSUNG, "A", "보고서", "20261101", "x", 4)
    ]))
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert summary.given_up == 1 and summary.failed == 0
    assert load_state(s.ingest_state_file).given_up[0].rcept_no == "R1"


def test_list_failure_skips_only_that_pair(tmp_path):
    class Fake(FakeIndex):
        def list_disclosures(self, corp_code, bgn_de, end_de, pblntf_ty):
            if pblntf_ty == "A":
                raise RuntimeError("net")
            return super().list_disclosures(corp_code, bgn_de, end_de, pblntf_ty)

    fake = Fake({(SAMSUNG, "B"): [item("R1")]})
    runner, s = make(tmp_path, fake)
    save_state(s.ingest_state_file, IngestState(last_success_date=date(2026, 10, 1)))
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A", "B"], today=D2)
    assert summary.failed == 1 and summary.new == 1
    assert load_state(s.ingest_state_file).last_success_date == date(2026, 10, 1)


def test_list_rate_limit_aborts(tmp_path):
    class Fake(FakeIndex):
        def list_disclosures(self, *a):
            raise DartApiError("020", "limit")

    runner, _ = make(tmp_path, Fake())
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert summary.aborted_reason == "DART 하루 호출 한도 초과"


def test_pending_outside_filter_or_watchlist_is_skipped(tmp_path):
    fake = FakeIndex()
    runner, s = make(tmp_path, fake)
    save_state(s.ingest_state_file, IngestState(pending=[
        PendingFailure("R1", FNF, "A", "보고서", "20261101", "x", 1),
        PendingFailure("R2", "99999999", "A", "보고서", "20261101", "x", 1),
    ]))
    runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert fake.index_calls == []
    assert len(load_state(s.ingest_state_file).pending) == 2


def test_empty_ledger_with_existing_chunks_refuses_to_run(tmp_path):
    fake = FakeIndex({(SAMSUNG, "A"): [item("R1")]})
    runner, s = make(tmp_path, fake, chunk_count=lambda: 28362)
    save_state(s.ingest_state_file, IngestState(last_success_date=date(2026, 10, 1)))
    summary = runner.run(D1, D2, corp_codes=[SAMSUNG], types=["A"], today=D2)
    assert "import-ledger" in summary.aborted_reason
    assert fake.index_calls == [] and fake.list_calls == []
    state = load_state(s.ingest_state_file)
    assert state.last_success_date == date(2026, 10, 1) and state.last_run_at
