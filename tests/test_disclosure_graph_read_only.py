from neo4j import Query

from app.services.disclosure_graph import DisclosureGraphService


class _Session:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query):
        self.log["query"] = query
        return iter(self.rows)


class _Driver:
    def __init__(self, rows):
        self.log = {}
        self.rows = rows

    def session(self, **kwargs):
        self.log["session"] = kwargs
        return _Session(self.rows, self.log)


class _Rec(dict):
    """neo4j Record처럼 keys()와 인덱싱을 지원한다."""


def _records(n):
    return [_Rec(n=i) for i in range(n)]


def test_default_behavior_unchanged_plain_string_all_rows():
    driver = _Driver(_records(300))
    out = DisclosureGraphService(driver).run_read_only_query("MATCH (n) RETURN n")
    assert driver.log["query"] == "MATCH (n) RETURN n"
    assert out.count("\n") == 299 and "잘렸" not in out


def test_cap_and_timeout_applied_in_read_session():
    driver = _Driver(_records(500))
    out = DisclosureGraphService(driver).run_read_only_query(
        "MATCH (n) RETURN n", max_rows=200, timeout=10
    )
    assert driver.log["session"] == {"default_access_mode": "READ"}
    q = driver.log["query"]
    assert isinstance(q, Query) and q.text == "MATCH (n) RETURN n" and q.timeout == 10
    lines = out.splitlines()
    assert sum(1 for line in lines if line.startswith("- ")) == 200
    assert "200행" in lines[-1] and "잘렸" in lines[-1]


def test_cap_not_hit_has_no_note():
    out = DisclosureGraphService(_Driver(_records(200))).run_read_only_query(
        "q", max_rows=200, timeout=10
    )
    assert "잘렸" not in out
