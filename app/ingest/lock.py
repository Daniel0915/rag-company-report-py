"""수집 실행 중복을 막는 파일 잠금 (배치 CLI와 API가 동시에 수집하지 않도록)."""

from __future__ import annotations

import errno
import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class IngestBusyError(RuntimeError):
    pass


@contextmanager
def ingest_lock(path: str | Path) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # flock 잠금은 열린 파일 디스크립터마다 따로 잡히므로, 같은 프로세스 안의 두 번째 호출도 막힌다.
    with open(lock_path, "a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise IngestBusyError("이미 수집이 실행 중입니다") from e
            raise
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
