"""Safe expression evaluator for edge `when:` and condition `if:` clauses.

An AST whitelist, not a sandbox escape hazard: only literals, boolean
logic, comparisons, arithmetic, dotted lookups into the provided
namespace, indexing, and a few builtin functions are allowed. No
attribute access on arbitrary objects (lookups descend into
dicts/lists only), no calls except the whitelisted builtins, no
comprehensions, lambdas, or dunder access.

Namespace: bare names resolve to state channels first, then node ids
(yielding that node's output dict), so `score >= 0.8` reads state and
`score_draft.score >= 0.8` reads a node output.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

_ALLOWED_FUNCS: dict[str, Any] = {
    "len": len, "min": min, "max": max, "abs": abs, "round": round,
    "str": str, "int": int, "float": float, "bool": bool,
}

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.Name, ast.Load, ast.Attribute, ast.Subscript, ast.Constant,
    ast.List, ast.Tuple, ast.Dict, ast.Call, ast.keyword,
)


class ExprError(ValueError):
    pass


def _check(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExprError("underscore attributes are not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise ExprError("only builtin functions len/min/max/abs/round/str/int/float/bool may be called")


class _Evaluator(ast.NodeVisitor):
    def __init__(self, namespace: Mapping[str, Any]):
        self.ns = namespace

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.ns:
            return self.ns[node.id]
        if node.id in _ALLOWED_FUNCS:
            return _ALLOWED_FUNCS[node.id]
        raise ExprError(f"unknown name '{node.id}'")

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        obj = self.visit(node.value)
        if isinstance(obj, Mapping) and node.attr in obj:
            return obj[node.attr]
        raise ExprError(f"cannot resolve '.{node.attr}'")

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        obj = self.visit(node.value)
        key = self.visit(node.slice)
        try:
            return obj[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExprError(f"bad subscript [{key!r}]") from exc

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return result
            return result
        for value in node.values:
            result = self.visit(value)
            if result:
                return result
        return result

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        return -operand

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left, right = self.visit(node.left), self.visit(node.right)
        ops = {ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
               ast.Mult: lambda: left * right, ast.Div: lambda: left / right,
               ast.FloorDiv: lambda: left // right, ast.Mod: lambda: left % right}
        return ops[type(node.op)]()

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            ok = {ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                  ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                  ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                  ast.In: lambda: left in right, ast.NotIn: lambda: left not in right,
                  ast.Is: lambda: left is right, ast.IsNot: lambda: left is not right,
                  }[type(op)]()
            if not ok:
                return False
            left = right
        return True

    def visit_List(self, node: ast.List) -> list:
        return [self.visit(e) for e in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> tuple:
        return tuple(self.visit(e) for e in node.elts)

    def visit_Dict(self, node: ast.Dict) -> dict:
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def visit_Call(self, node: ast.Call) -> Any:
        func = self.visit(node.func)
        args = [self.visit(a) for a in node.args]
        return func(*args)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ExprError(f"disallowed syntax: {type(node).__name__}")


def evaluate(expr: str, namespace: Mapping[str, Any]) -> Any:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"invalid expression: {expr!r}") from exc
    _check(tree)
    return _Evaluator(namespace).visit(tree)


def build_namespace(state: Mapping[str, Any], node_outputs: Mapping[str, Any]) -> dict[str, Any]:
    ns = dict(node_outputs)
    ns.update(state)  # state channels shadow node ids on collision
    return ns
