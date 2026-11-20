"""`python -m app.eval` 명령줄: 정답셋 후보 승인(accept). 이후 generate, run, compare가 붙는다."""

from __future__ import annotations

import argparse

from neo4j import GraphDatabase

from app.config import get_settings
from app.eval.dataset import DatasetError, accept
from app.eval.generate import DEFAULT_QUOTA, EVAL_CLAUDE_TIMEOUT_SECONDS, generate, parse_quota
from app.llm.claude_cli import ClaudeCliClient
from app.watchlist import find

DEFAULT_CANDIDATES = "eval/candidates.yaml"
DEFAULT_DATASET = "eval/dataset.yaml"


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
    return 0


_COMMANDS = {"accept": _cmd_accept, "generate": _cmd_generate}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except DatasetError as exc:
        print(f"오류: {exc}")
        return 1
