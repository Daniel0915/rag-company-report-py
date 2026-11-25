"""평가 실행(`run`): 정답셋 문항을 실제 채팅 경로(검색 -> 답변)로 돌려 채점하고 결과 파일을 만든다."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime

from neo4j import Driver

from app.eval.dataset import Item
from app.llm.claude_cli import CliCall, metered
from app.agent.service import AgentService
from app.eval.metrics import (
    Retrieved,
    aggregate,
    aggregate_by_type,
    retrieved_from_records,
    score_item,
    score_tools,
)
from app.services.company_chat import CompanyChatService
from app.vector.document import Document
from app.vector.registry import label_for

_SNAPSHOT_QUERY = """
MATCH (n:`{label}`)
WHERE n.`metadata.doc_type` IS NULL
RETURN count(n) AS chunks, max(n.`metadata.rcept_dt`) AS latest
"""


def to_retrieved(doc: Document) -> Retrieved:
    meta = doc.metadata
    return Retrieved(
        rcept_no=meta.get("rcept_no"),
        source_file=meta.get("source_file"),
        section_order=meta.get("section_order"),
        section_title=meta.get("section_title"),
        score=doc.score,
        text=doc.text,
        corp_name=meta.get("corp_name"),
        report_nm=meta.get("report_nm"),
    )


def _retrieved_rows(retrieved: list[Retrieved]) -> list[dict]:
    return [
        {
            "rcept_no": r.rcept_no,
            "source_file": r.source_file,
            "section_order": r.section_order,
            "section_title": r.section_title,
            "score": r.score,
        }
        for r in retrieved
    ]


def _empty_row(item: Item) -> dict:
    return {
        "id": item.id,
        "corp_code": item.corp_code,
        "pblntf_ty": item.pblntf_ty,
        "answerable": item.answerable,
        "question": item.question,
        "retrieved": [],
        "answer": None,
        "prompt_tokens": None,
        "metrics": None,
        "judge": None,
        "error": None,
        "error_stage": None,
        "usage": None,
    }


def usage_row(calls: list[CliCall]) -> dict | None:
    """답을 만드는 동안 일어난 claude 호출의 토큰과 비용 합계. 채점 호출은 넣지 않는다. claude를 안 썼으면 None."""
    if not calls:
        return None
    return {
        "calls": len(calls),
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cost_usd": sum(c.cost_usd for c in calls),
    }


def run_item(
    chat: CompanyChatService, item: Item, top_k: int, provider: str, judge_fn: Callable | None
) -> dict:
    """문항 하나를 돌려 결과 행을 만든다. 어떤 예외든 error와 error_stage(retrieve|answer|score)로 남기고 metrics는 None으로 둔다."""
    row = _empty_row(item)
    stage = "retrieve"
    try:
        with metered() as calls:
            docs = chat.retrieve(item.question, item.corp_code, top_k)
            retrieved = [to_retrieved(d) for d in docs]
            row["retrieved"] = _retrieved_rows(retrieved)
            stage = "answer"
            answer = chat.answer(item.question, item.corp_code, docs, provider)
        row["usage"] = usage_row(calls)
        row["answer"] = answer.text
        row["prompt_tokens"] = answer.prompt_tokens
        stage = "score"
        row["metrics"] = score_item(item, retrieved, answer.text)
        result = judge_fn(item, retrieved, answer.text) if judge_fn else None
        row["judge"] = asdict(result) if result else None
    except Exception as exc:  # noqa: BLE001 - 한 문항의 실패가 전체 실행을 멈추지 않게 한다
        row["metrics"] = None
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["error_stage"] = stage
    return row


def run_agent_item(agent: AgentService, item: Item, provider: str, judge_fn: Callable | None) -> dict:
    """에이전트 경로로 문항 하나를 돌린다. 오류 단계는 agent|score. 검색 지표는 search_disclosures 청크로 센다."""
    row = _empty_row(item) | {"tool_calls": []}
    stage = "agent"
    try:
        with metered() as calls:
            result = agent.run(item.question, item.corp_code, provider)
        row["usage"] = usage_row(calls)
        retrieved = retrieved_from_records(result.records)
        row["retrieved"] = _retrieved_rows(retrieved)
        row["tool_calls"] = [
            {"seq": r.seq, "name": r.name, "arguments": r.arguments, "ok": r.ok} for r in result.records
        ]
        row["answer"] = result.answer
        row["prompt_tokens"] = result.prompt_tokens
        stage = "score"
        row["metrics"] = score_item(
            item, retrieved, result.answer, context_texts=[r.result_text for r in result.records]
        ) | score_tools(item, result.records)
        judged = (
            judge_fn(
                item, retrieved, result.answer, evidence_texts=[r.result_text for r in result.records]
            )
            if judge_fn
            else None
        )
        row["judge"] = asdict(judged) if judged else None
    except Exception as exc:  # noqa: BLE001 - 한 문항의 실패가 전체 실행을 멈추지 않게 한다
        row["metrics"] = None
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["error_stage"] = stage
    return row


def index_snapshot(driver: Driver, corp_codes: set[str]) -> dict[str, dict]:
    """기업마다 공시 청크 수와 가장 최근 접수일. 실행 시점의 인덱스 상태를 결과에 남기려는 것이다."""
    snapshot: dict[str, dict] = {}
    for corp_code in sorted(corp_codes):
        with driver.session() as session:
            record = list(session.run(_SNAPSHOT_QUERY.format(label=label_for(corp_code))))[0]
        snapshot[corp_code] = {"chunks": record["chunks"], "latest_rcept_dt": record["latest"]}
    return snapshot


def run(
    items: list[Item],
    chat: CompanyChatService,
    top_k: int,
    provider: str,
    judge_fn: Callable | None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    rows = []
    for number, item in enumerate(items, start=1):
        if on_progress:
            on_progress(number, len(items), item.id)
        rows.append(run_item(chat, item, top_k, provider, judge_fn))
    return rows


def run_agent(
    items: list[Item],
    agent: AgentService,
    provider: str,
    judge_fn: Callable | None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    rows = []
    for number, item in enumerate(items, start=1):
        if on_progress:
            on_progress(number, len(items), item.id)
        rows.append(run_agent_item(agent, item, provider, judge_fn))
    return rows


def build_run_file(config: dict, index: dict, rows: list[dict]) -> dict:
    return {
        "config": config,
        "index": index,
        "items": rows,
        "summary": {"overall": aggregate(rows), "by_type": aggregate_by_type(rows)},
    }


def run_file_name(started: datetime, provider: str, top_k: int, agent: bool = False) -> str:
    return f"{started:%Y%m%d-%H%M%S}_{provider}_k{top_k}{'_agent' if agent else ''}.json"


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _table(metrics: dict[str, dict]) -> list[str]:
    width = max((len(name) for name in metrics), default=4)
    lines = [f"{'지표'.ljust(width)} | {'값':>8} | 분모"]
    for name, stat in sorted(metrics.items()):
        lines.append(f"{name.ljust(width)} | {stat['mean']:>8.3f} | {stat['n']}")
    return lines


def format_summary(run_file: dict) -> str:
    summary = run_file["summary"]
    lines = _table(summary["overall"])
    for ty, metrics in sorted(summary["by_type"].items()):
        lines += ["", f"[{ty}]", *_table(metrics)]
    return "\n".join(lines)
