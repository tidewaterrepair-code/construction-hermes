"""Safe arithmetic evaluator for assembly quantity formulas (no eval, no attribute access)."""

from __future__ import annotations

import ast
import math
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Mapping

from .money import D


class FormulaError(ValueError):
    pass


def _ceil(x: Decimal) -> Decimal:
    return D(x).to_integral_value(rounding=ROUND_CEILING)


def _floor(x: Decimal) -> Decimal:
    return D(x).to_integral_value(rounding=ROUND_FLOOR)


def _round(x: Decimal, n: Decimal = Decimal(0)) -> Decimal:
    return D(x).quantize(Decimal(1).scaleb(-int(n)), rounding=ROUND_HALF_UP)


_FUNCS = {"ceil": _ceil, "floor": _floor, "round": _round, "max": max, "min": min,
          "sqrt": lambda x: D(math.sqrt(D(x)))}
_MAX_LEN = 300


def evaluate(expr: str, params: Mapping[str, Decimal]) -> Decimal:
    if len(expr) > _MAX_LEN:
        raise FormulaError("formula too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"invalid formula: {expr}") from exc

    def ev(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)) and not isinstance(node.value, bool):
            return D(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            return D(repr(node.value))
        if isinstance(node, ast.Name):
            if node.id not in params:
                raise FormulaError(f"missing parameter: {node.id}")
            v = params[node.id]
            if v is None:
                raise FormulaError(f"missing parameter: {node.id}")
            return D(v)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                if b == 0:
                    raise FormulaError("division by zero")
                return a / b
            raise FormulaError("operator not allowed")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return D(_FUNCS[node.func.id](*[ev(a) for a in node.args]))
        raise FormulaError(f"unsupported expression element: {type(node).__name__}")

    return ev(tree)


def required_names(expr: str) -> set[str]:
    tree = ast.parse(expr, mode="eval")
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _FUNCS}
