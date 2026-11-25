"""두 실행 결과 파일 비교: 지표별 변화와 문항별로 좋아지거나 나빠진 기준."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Change:
    id: str
    criterion: str
    before: object
    after: object


# 지표별로 클수록 좋은가. 여기 없는 지표(prompt_tokens)는 방향을 매기지 않는다(컨텍스트를 늘렸을 수 있다).
HIGHER_IS_BETTER: dict[str, bool] = {
    "hit_strict": True,
    "hit_loose": True,
    "mrr": True,
    "fact_recall": True,
    "correct_refusal": True,
    "faithfulness": True,
    "judge_correct": True,
    "false_refusal": False,
    "unsupported_numbers": False,
    "tool_selection": True,
    "unnecessary_web": False,
    "tool_errors": False,
}


@dataclass
class Comparison:
    metric_deltas: dict[str, tuple[float | None, float | None]]
    metric_counts: dict[str, tuple[int | None, int | None]]
    improved: list[Change]
    regressed: list[Change]
    dataset_changed: bool
    index_changes: dict[str, tuple[dict | None, dict | None]]
    only_in_a: list[str]
    only_in_b: list[str]


def _criteria(row: dict) -> dict[str, tuple[object, bool]]:
    """기준 이름 -> (값, 클수록 좋은가). 값이 없는 기준은 뺀다."""
    metrics = row.get("metrics") or {}
    values: dict[str, tuple[object, bool]] = {}
    if "hit_strict" in metrics:
        values["hit_strict"] = (metrics["hit_strict"], True)
    if "fact_recall" in metrics:
        values["fact_recall"] = (metrics["fact_recall"], True)
    if row.get("answerable"):
        if "false_refusal" in metrics:
            values["refusal"] = (metrics["false_refusal"], False)
    elif "correct_refusal" in metrics:
        values["refusal"] = (metrics["correct_refusal"], True)
    if row.get("judge"):
        values["judge_correct"] = (row["judge"]["correct"], True)
    return values


def _compare_item(item_id: str, ra: dict, rb: dict, improved: list[Change], regressed: list[Change]) -> None:
    error_a, error_b = ra.get("error"), rb.get("error")
    if error_a or error_b:
        if error_b and not error_a:
            regressed.append(Change(item_id, "error", None, error_b))
        elif error_a and not error_b:
            improved.append(Change(item_id, "error", error_a, None))
        return
    crit_a, crit_b = _criteria(ra), _criteria(rb)
    for name in crit_a.keys() & crit_b.keys():
        before, higher_is_better = crit_a[name]
        after, _ = crit_b[name]
        if before == after:
            continue
        better = (after > before) == higher_is_better
        (improved if better else regressed).append(Change(item_id, name, before, after))


def compare(a: dict, b: dict) -> Comparison:
    overall_a, overall_b = a["summary"]["overall"], b["summary"]["overall"]
    metric_deltas = {
        name: (
            overall_a[name]["mean"] if name in overall_a else None,
            overall_b[name]["mean"] if name in overall_b else None,
        )
        for name in sorted(overall_a.keys() | overall_b.keys())
    }
    metric_counts = {
        name: (
            overall_a[name]["n"] if name in overall_a else None,
            overall_b[name]["n"] if name in overall_b else None,
        )
        for name in metric_deltas
    }
    rows_a = {row["id"]: row for row in a["items"]}
    rows_b = {row["id"]: row for row in b["items"]}
    improved: list[Change] = []
    regressed: list[Change] = []
    for item_id in rows_a:
        if item_id in rows_b:
            _compare_item(item_id, rows_a[item_id], rows_b[item_id], improved, regressed)
    index_a, index_b = a.get("index", {}), b.get("index", {})
    index_changes = {
        corp: (index_a.get(corp), index_b.get(corp))
        for corp in sorted(index_a.keys() | index_b.keys())
        if index_a.get(corp) != index_b.get(corp)
    }
    return Comparison(
        metric_deltas=metric_deltas,
        metric_counts=metric_counts,
        improved=improved,
        regressed=regressed,
        dataset_changed=a["config"].get("dataset_hash") != b["config"].get("dataset_hash"),
        index_changes=index_changes,
        only_in_a=[i for i in rows_a if i not in rows_b],
        only_in_b=[i for i in rows_b if i not in rows_a],
    )


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _verdict(name: str, before: float | None, after: float | None) -> str:
    if before is None or after is None or before == after or name not in HIGHER_IS_BETTER:
        return ""
    return "좋아짐" if (after > before) == HIGHER_IS_BETTER[name] else "나빠짐"


def _fmt_n(n: int | None) -> str:
    return "-" if n is None else str(n)


def _fmt_index(state: dict | None) -> str:
    if state is None:
        return "없음"
    return f"청크 {state.get('chunks')}, 최근 접수일 {state.get('latest_rcept_dt')}"


def _fmt_changes(title: str, changes: list[Change]) -> list[str]:
    lines = ["", f"{title} ({len(changes)})"]
    lines += [f"  {c.id} [{c.criterion}] {c.before} -> {c.after}" for c in sorted(changes, key=lambda c: (c.id, c.criterion))]
    return lines


def format_comparison(c: Comparison) -> str:
    lines: list[str] = []
    if c.dataset_changed:
        lines.append("경고: 두 실행의 정답셋이 다릅니다 (비교 결과를 그대로 믿기 어렵습니다)")
    if c.index_changes:
        lines.append("경고: 두 실행 사이에 인덱스가 바뀌었습니다")
        for corp, (before, after) in c.index_changes.items():
            lines.append(f"  {corp}: {_fmt_index(before)} -> {_fmt_index(after)}")
    if lines:
        lines.append("")
    width = max((len(name) for name in c.metric_deltas), default=4)
    lines.append(f"{'지표'.ljust(width)} | {'A':>8} | {'B':>8} | {'차이':>8} | {'n_A':>4} | {'n_B':>4} | 판정")
    for name, (before, after) in c.metric_deltas.items():
        diff = "-" if before is None or after is None else f"{after - before:+.3f}"
        n_a, n_b = (_fmt_n(n) for n in c.metric_counts[name])
        lines.append(
            f"{name.ljust(width)} | {_fmt(before):>8} | {_fmt(after):>8} | {diff:>8} | {n_a:>4} | {n_b:>4} | "
            f"{_verdict(name, before, after)}".rstrip()
        )
    lines += _fmt_changes("나빠진 문항", c.regressed)
    lines += _fmt_changes("좋아진 문항", c.improved)
    if c.only_in_a:
        lines += ["", "A에만 있는 문항: " + ", ".join(c.only_in_a)]
    if c.only_in_b:
        lines += ["", "B에만 있는 문항: " + ", ".join(c.only_in_b)]
    return "\n".join(lines)
