import pytest

from app.ingest.lock import IngestBusyError, ingest_lock


def test_second_acquire_raises_busy(tmp_path):
    p = tmp_path / "ingest.lock"
    with ingest_lock(p):
        with pytest.raises(IngestBusyError):
            with ingest_lock(p):
                pass


def test_stale_lock_file_does_not_block(tmp_path):
    p = tmp_path / "ingest.lock"
    p.write_text("")
    with ingest_lock(p):
        pass


def test_lock_released_after_exception(tmp_path):
    p = tmp_path / "ingest.lock"
    with pytest.raises(ValueError):
        with ingest_lock(p):
            raise ValueError
    with ingest_lock(p):
        pass
