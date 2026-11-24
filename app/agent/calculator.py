"""사칙연산 계산기. eval 없이 ast를 직접 순회해 허용한 노드만 계산한다."""
from __future__ import annotations

import ast
import operator

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def calculate(expression: str) -> float:
    """숫자 상수, + - * /, 단항 -, 괄호만 허용한다. 그 밖의 식과 0으로 나누기는 ValueError."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise ValueError("수식을 해석할 수 없습니다") from None
    return float(_eval(tree.body))


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        try:
            return _BINARY[type(node.op)](left, right)
        except ZeroDivisionError:
            raise ValueError("0으로 나눌 수 없습니다") from None
    raise ValueError("허용되지 않는 수식입니다 (숫자와 + - * / 괄호만 사용)")
