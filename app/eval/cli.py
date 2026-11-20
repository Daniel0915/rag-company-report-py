"""`python -m app.eval` 명령줄: accept, generate, run, compare."""

from __future__ import annotations

import argparse
import functools
import json
from datetime import datetime
from pathlib import Path

from neo4j import GraphDatabase

from app.config import get_settings
from app.eval.compare import compare, format_comparison
from app.eval.dataset import DatasetError, accept, file_hash, load_items
from app.eval.generate import DEFAULT_QUOTA, EVAL_CLAUDE_TIMEOUT_SECONDS, generate, parse_quota
from app.eval.judge import judge
from app.eval.runner import build_run_file, format_summary, git_commit, index_snapshot, run, run_file_name
from app.llm.claude_cli import ClaudeCliClient
from app.llm.gemini import GeminiClient
from app.llm.ollama import OllamaClient
from app.services.company_chat import CompanyChatService
from app.services.disclosure_graph import DisclosureGraphService
from app.vector.registry import CompanyVectorStoreRegistry
from app.watchlist import find

DEFAULT_CANDIDATES = "eval/candidates.yaml"
DEFAULT_DATASET = "eval/dataset.yaml"
DEFAULT_RUNS_DIR = "eval/runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.eval", description="RAG 평가")
    sub = parser.add_subparsers(dest="command", required=True)

    p_accept = sub.add_parser("accept", help="review가 ok인 후보를 정답셋으로 옮기고 drop은 버린다")
    p_accept.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    p_accept.add_argument("--dataset", default=DEFAULT_DATASET)

    p_gen = sub.add_parser("generate", help="공시 유형별 할당으로 청크를 뽑아 Claude로 후보 문항을 만든다")
    p_gen.add_argument("--corp", required=True, help="워치리스트 기업의 corp_code")
    p_gen.add_argument("--quota", help="유형별 개수 (예: A=6,I=4). 지정한 유형만 뽑는다")
    p_gen.add_argument("--seed", type=int)
    p_gen.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    p_gen.add_argument("--dataset", default=DEFAULT_DATASET)

    p_run = sub.add_parser("run", help="정답셋을 실제 채팅 경로로 돌려 채점하고 결과 파일을 남긴다")
    p_run.add_argument("--provider", choices=["local", "gemini", "claude"], default="local")
    p_run.add_argument("--top-k", type=int, default=6)
    p_run.add_argument("--judge", choices=["none", "claude"], default="none")
    p_run.add_argument("--temperature", type=float, default=0)
    p_run.add_argument("--seed", type=int, default=42)
    p_run.add_argument("--dataset", default=DEFAULT_DATASET)
    p_run.add_argument("--out-dir", default=DEFAULT_RUNS_DIR)
    p_run.add_argument("--corp", help="이 기업의 문항만 돌린다")

    p_cmp = sub.add_parser("compare", help="두 결과 파일의 지표와 문항별 변화를 비교한다")
    p_cmp.add_argument("run_a")
    p_cmp.add_argument("run_b")
    return parser


def _cmd_accept(args: argparse.Namespace) -> int:
    moved, dropped = accept(args.candidates, args.dataset)
    print(f"옮김 {moved}, 버림 {dropped}")
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    if find(args.corp) is None:
        print(f"오류: 워치리스트에 없는 기업입니다 ({args.corp})")
        return 1
    try:
        quota = parse_quota(args.quota) if args.quota else dict(DEFAULT_QUOTA)
    except ValueError as exc:
        print(f"오류: {exc}")
        return 1
    settings = get_settings()
    claude = ClaudeCliClient(settings.model_copy(update={"claude_timeout_seconds": EVAL_CLAUDE_TIMEOUT_SECONDS}))
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password))
    try:
        result = generate(driver, claude, args.corp, quota, args.seed, args.candidates, args.dataset)
    finally:
        driver.close()
    picked = result["picked"]
    print("뽑은 수: " + ", ".join(f"{ty} {n}" for ty, n in sorted(picked.items())))
    for ty, want in sorted(quota.items()):
        if ty != "A" and picked.get(ty, 0) < want:
            print(f"{ty}: 할당 {want}, 뽑음 {picked.get(ty, 0)} (부족분 A에서 채움)")
    print(f"추가 {result['added']}, 파싱 실패 {result['parse_failed']}, 호출 실패 {result['call_failed']}")
    if result["call_failed"]:
        print(f"첫 호출 오류: {result['first_error']}")
    return 0


def _chat_model(settings, provider: str) -> str:
    return {
        "local": settings.ollama_chat_model,
        "gemini": settings.gemini_model,
        "claude": settings.claude_model,
    }[provider]


def _cmd_run(args: argparse.Namespace) -> int:
    items = load_items(args.dataset)
    if args.corp:
        items = [item for item in items if item.corp_code == args.corp]
    if not items:
        print("정답셋이 비어 있습니다")
        return 1
    if args.provider == "claude" and args.judge == "claude":
        print("경고: 답변과 채점 모두 claude라 자기 답을 자기가 채점합니다")

    settings = get_settings()
    started = datetime.now()
    ollama = OllamaClient(settings, options={"temperature": args.temperature, "seed": args.seed})
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password))
    try:
        chat = CompanyChatService(
            ollama,
            GeminiClient(settings),
            ClaudeCliClient(settings),
            CompanyVectorStoreRegistry(driver, ollama, settings),
            DisclosureGraphService(driver),
        )
        judge_fn = None
        if args.judge == "claude":
            judge_claude = ClaudeCliClient(settings.model_copy(update={"claude_timeout_seconds": EVAL_CLAUDE_TIMEOUT_SECONDS}))
            judge_fn = functools.partial(judge, judge_claude)
        index = index_snapshot(driver, {item.corp_code for item in items})
        rows = run(
            items,
            chat,
            args.top_k,
            args.provider,
            judge_fn,
            on_progress=lambda n, total, item_id: print(f"[{n}/{total}] {item_id} ...", flush=True),
        )
    finally:
        driver.close()
        ollama.close()

    config = {
        "provider": args.provider,
        "top_k": args.top_k,
        "chat_model": _chat_model(settings, args.provider),
        "embedding_model": settings.ollama_embedding_model,
        "judge": args.judge,
        "temperature": args.temperature,
        "seed": args.seed,
        "dataset_path": args.dataset,
        "dataset_hash": file_hash(args.dataset),
        "git_commit": git_commit(),
        "started_at": started.isoformat(),
    }
    run_file = build_run_file(config, index, rows)
    out_path = Path(args.out_dir) / run_file_name(started, args.provider, args.top_k)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(run_file, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"결과 파일: {out_path}")
    print(format_summary(run_file))
    return 0


def _load_run_file(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["config"], data["summary"]["overall"], data["items"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DatasetError(f"결과 파일을 읽을 수 없습니다 ({path}): {exc}") from exc
    return data


def _cmd_compare(args: argparse.Namespace) -> int:
    a = _load_run_file(args.run_a)
    b = _load_run_file(args.run_b)
    print(format_comparison(compare(a, b)))
    return 0


_COMMANDS = {"accept": _cmd_accept, "generate": _cmd_generate, "run": _cmd_run, "compare": _cmd_compare}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except DatasetError as exc:
        print(f"오류: {exc}")
        return 1
