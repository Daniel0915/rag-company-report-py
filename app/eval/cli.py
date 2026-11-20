"""`python -m app.eval` 명령줄: 정답셋 후보 승인(accept). 이후 generate, run, compare가 붙는다."""

from __future__ import annotations

import argparse

from app.eval.dataset import DatasetError, accept

DEFAULT_CANDIDATES = "eval/candidates.yaml"
DEFAULT_DATASET = "eval/dataset.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.eval", description="RAG 평가")
    sub = parser.add_subparsers(dest="command", required=True)

    p_accept = sub.add_parser("accept", help="review가 ok인 후보를 정답셋으로 옮기고 drop은 버린다")
    p_accept.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    p_accept.add_argument("--dataset", default=DEFAULT_DATASET)
    return parser


def _cmd_accept(args: argparse.Namespace) -> int:
    moved, dropped = accept(args.candidates, args.dataset)
    print(f"옮김 {moved}, 버림 {dropped}")
    return 0


_COMMANDS = {"accept": _cmd_accept}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except DatasetError as exc:
        print(f"오류: {exc}")
        return 1
