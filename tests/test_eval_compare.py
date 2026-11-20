import json

from app.eval.cli import main
from app.eval.compare import compare, format_comparison


def _row(item_id, answerable=True, error=None, judge=None, **metrics):
    return {
        "id": item_id,
        "answerable": answerable,
        "metrics": None if error else metrics,
        "judge": judge,
        "error": error,
    }


def _run(rows, dataset_hash="h1", index=None, overall=None):
    return {
        "config": {"dataset_hash": dataset_hash},
        "index": index if index is not None else {},
        "items": rows,
        "summary": {"overall": overall or {}, "by_type": {}},
    }


def test_regression_on_hit_and_refusal():
    a = _run([_row("q1", hit_strict=1, fact_recall=1.0, false_refusal=0), _row("m1", False, correct_refusal=1)])
    b = _run([_row("q1", hit_strict=0, fact_recall=1.0, false_refusal=0), _row("m1", False, correct_refusal=0)])
    c = compare(a, b)
    assert {(x.id, x.criterion) for x in c.regressed} == {("q1", "hit_strict"), ("m1", "refusal")}
    assert c.improved == []


def test_improvement_and_fact_recall_and_false_refusal():
    a = _run([_row("q1", hit_strict=0, fact_recall=0.5, false_refusal=1)])
    b = _run([_row("q1", hit_strict=1, fact_recall=1.0, false_refusal=0)])
    c = compare(a, b)
    assert {(x.id, x.criterion) for x in c.improved} == {("q1", "hit_strict"), ("q1", "fact_recall"), ("q1", "refusal")}
    assert c.regressed == []
    worse = compare(b, a)
    assert {x.criterion for x in worse.regressed} == {"hit_strict", "fact_recall", "refusal"}


def test_judge_correct_only_when_both_have_judge():
    ja = {"correct": True, "faithfulness": 1, "reason": ""}
    jb = {"correct": False, "faithfulness": 1, "reason": ""}
    both = compare(_run([_row("q1", judge=ja, hit_strict=1)]), _run([_row("q1", judge=jb, hit_strict=1)]))
    assert [(x.id, x.criterion) for x in both.regressed] == [("q1", "judge_correct")]
    one = compare(_run([_row("q1", judge=ja, hit_strict=1)]), _run([_row("q1", judge=None, hit_strict=1)]))
    assert one.regressed == [] and one.improved == []


def test_new_error_counts_as_regression():
    a = _run([_row("q1", hit_strict=1)])
    b = _run([_row("q1", error="boom")])
    c = compare(a, b)
    assert [(x.id, x.criterion) for x in c.regressed] == [("q1", "error")]
    fixed = compare(b, a)
    assert [(x.id, x.criterion) for x in fixed.improved] == [("q1", "error")]


def test_dataset_hash_and_index_changes_are_reported():
    a = _run([], "h1", {"00126380": {"chunks": 33127, "latest_rcept_dt": "20261101"}, "x": {"chunks": 1}})
    b = _run([], "h2", {"00126380": {"chunks": 33500, "latest_rcept_dt": "20261110"}, "x": {"chunks": 1}})
    c = compare(a, b)
    assert c.dataset_changed and list(c.index_changes) == ["00126380"]
    text = format_comparison(c)
    assert "경고: 두 실행의 정답셋이 다릅니다" in text
    assert "33127" in text and "33500" in text


def test_items_only_in_one_run_are_listed():
    a = _run([_row("q1", hit_strict=1), _row("q2", hit_strict=1)])
    b = _run([_row("q1", hit_strict=1), _row("q3", hit_strict=1)])
    c = compare(a, b)
    assert c.only_in_a == ["q2"] and c.only_in_b == ["q3"]


def test_metric_deltas_with_missing_side():
    a = _run([], overall={"hit_strict": {"mean": 0.5, "n": 2}})
    b = _run([], overall={"hit_strict": {"mean": 0.75, "n": 2}, "faithfulness": {"mean": 0.9, "n": 2}})
    c = compare(a, b)
    assert c.metric_deltas == {"hit_strict": (0.5, 0.75), "faithfulness": (None, 0.9)}
    text = format_comparison(c)
    assert "hit_strict" in text and "+0.250" in text


def test_cli_compare(tmp_path, capsys):
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(_run([_row("q1", hit_strict=1)])), encoding="utf-8")
    pb.write_text(json.dumps(_run([_row("q1", hit_strict=0)])), encoding="utf-8")
    assert main(["compare", str(pa), str(pb)]) == 0
    assert "q1" in capsys.readouterr().out


def test_cli_compare_bad_files(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["compare", str(tmp_path / "none.json"), str(bad)]) == 1
    assert main(["compare", str(bad), str(bad)]) == 1
    assert capsys.readouterr().out.count("오류") == 2


def test_format_shows_n_and_direction_markers():
    a = _run([], overall={
        "hit_strict": {"mean": 0.5, "n": 20}, "false_refusal": {"mean": 0.1, "n": 20},
        "prompt_tokens": {"mean": 2000.0, "n": 20}, "mrr": {"mean": 0.4, "n": 20},
    })
    b = _run([], overall={
        "hit_strict": {"mean": 0.6, "n": 18}, "false_refusal": {"mean": 0.2, "n": 18},
        "prompt_tokens": {"mean": 2500.0, "n": 18}, "mrr": {"mean": 0.4, "n": 18},
    })
    lines = {line.split("|")[0].strip(): line for line in format_comparison(compare(a, b)).splitlines() if "|" in line}
    assert "n_A" in lines["지표"] and "n_B" in lines["지표"]
    assert "20" in lines["hit_strict"] and "18" in lines["hit_strict"]
    assert "좋아짐" in lines["hit_strict"]
    assert "나빠짐" in lines["false_refusal"]
    assert "좋아짐" not in lines["prompt_tokens"] and "나빠짐" not in lines["prompt_tokens"]
    assert "좋아짐" not in lines["mrr"] and "나빠짐" not in lines["mrr"]
