"""算数工具（LEGACY / 已退役）：用 AST 安全地计算数学表达式，绝不使用 eval。

注意：当前版本不再把本工具注册给模型，算数改由通用工具 run_command 完成。
本文件仅作历史保留，见标签 v1-specialized-tools。
"""
import ast
import operator

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("表达式包含不支持的语法")


def calculate(expression: str) -> str:
    """安全地计算数学表达式，返回结果字符串。"""
    expression = (expression or "").strip()
    try:
        result = _eval(ast.parse(expression, mode="eval"))
    except ZeroDivisionError:
        return "错误：除数不能为 0。"
    except (ValueError, SyntaxError) as e:
        return f"错误：无法计算「{expression}」（{e}）。只支持数字和 + - * / ** % ()。"

    if isinstance(result, float) and result.is_integer():
        result = int(result)
    elif isinstance(result, float):
        result = round(result, 10)
    return f"{expression} = {result}"
