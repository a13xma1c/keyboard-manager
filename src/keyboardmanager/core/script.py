"""Expressions and scripts: a small, safe language with Python's syntax.

Expressions (conditions, ``{= ...}`` placeholders, Set variable "expression"):

    len(clipboard) > 10 and "Mail" in app
    upper(name) + "!"          round(price * 1.2, 2)          "yes" if n > 3 else "no"

Scripts (the "Run script" step) add statements: assignments (``x = 1``,
``x += 1``), ``if`` / ``elif`` / ``else``, ``while``, ``for x in ...``, ``break``,
``continue``, ``pass``, and commands such as ``type("hi")``, ``press("enter")``,
``wait(500)``, ``run("Other macro")``.

Only the listed constructs and functions exist: no imports, no attribute access,
no function definitions, no access to files or to Python itself. Every statement
and loop round is a pause / stop point and counts towards the step limit.

Variables are shared with the macro as text. Reading one gives a number when the
text is a plain number ("42", "-1.5"), true / false for "true" / "false", else the
text. Inside a script a variable keeps its real value (a list stays a list) until
the script ends.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import random
import re
from typing import Any, Callable

MAX_TEXT = 1_000_000  # longest string / list an expression may build
MAX_INT_BITS = 4096   # largest whole number (so 9 ** 9 ** 9 cannot stall a macro)


class ScriptError(ValueError):
    """A problem in an expression or script (reported with its line when known)."""


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


# --------------------------------------------------------------- conversions
_NUMBER = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")


def to_value(text: Any) -> Any:
    """A stored (text) variable as a value: plain numbers and true / false convert."""
    if not isinstance(text, str):
        return text
    if _NUMBER.match(text):
        return float(text) if "." in text else int(text)
    if text in ("true", "false"):
        return text == "true"
    return text


def to_text(value: Any) -> str:
    """A value as the text a variable stores."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        from keyboardmanager.core.variables import format_number

        return format_number(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return str(value)


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no")
    return bool(value)


# ----------------------------------------------------------------- functions
def _substr(text: Any, start: int, length: int | None = None) -> str:
    text = to_text(text)
    return text[start:] if length is None else text[start:start + length]


def _range(*args: Any) -> list[int]:
    numbers = range(*(int(_number(a)) for a in args))
    if len(numbers) > MAX_TEXT:  # checked before the list is built
        raise ScriptError("the range is too long")
    return list(numbers)


def _number(value: Any) -> float | int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    converted = to_value(to_text(value).strip())
    if isinstance(converted, bool) or not isinstance(converted, (int, float)):
        raise ScriptError(f"{to_text(value)!r} is not a number")
    return converted


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": lambda x: len(x if isinstance(x, list) else to_text(x)),
    "upper": lambda s: to_text(s).upper(),
    "lower": lambda s: to_text(s).lower(),
    "trim": lambda s: to_text(s).strip(),
    "replace": lambda s, old, new: to_text(s).replace(to_text(old), to_text(new)),
    "contains": lambda s, part: to_text(part).lower() in to_text(s).lower(),
    "startswith": lambda s, part: to_text(s).startswith(to_text(part)),
    "endswith": lambda s, part: to_text(s).endswith(to_text(part)),
    "substr": _substr,
    "split": lambda s, sep=None: to_text(s).split(to_text(sep)) if sep is not None else to_text(s).split(),
    "join": lambda items, sep="": to_text(sep).join(to_text(i) for i in items),
    "lines": lambda s: to_text(s).splitlines(),
    "matches": lambda s, pattern: re.search(to_text(pattern), to_text(s)) is not None,
    "number": _number,
    "text": to_text,
    "round": lambda x, digits=0: round(_number(x), int(digits)) if digits else round(_number(x)),
    "abs": lambda x: abs(_number(x)),
    "min": lambda *xs: min(xs[0] if len(xs) == 1 else xs),
    "max": lambda *xs: max(xs[0] if len(xs) == 1 else xs),
    "floor": lambda x: math.floor(_number(x)),
    "ceil": lambda x: math.ceil(_number(x)),
    "random": lambda low=1, high=100: random.randint(int(low), int(high)),
    "range": lambda *args: _range(*args),
    "empty": lambda s: not to_text(s).strip(),
}
FUNCTION_HELP = (
    "len, upper, lower, trim, replace(s, old, new), contains(s, part), startswith, endswith, "
    "substr(s, start, length), split(s, sep), join(list, sep), lines(s), matches(s, regex), "
    "number, text, round(x, digits), abs, min, max, floor, ceil, random(low, high), range, empty"
)
COMMAND_HELP = (
    'type("text"), paste("text"), press("ctrl+c"), hold("shift"), release("shift"), wait(ms), '
    'click(x, y, button="left"), move(x, y), scroll(dy), run("macro"), notify("message"), '
    'ask("question", default="") -> answer, log("note"), stop(), '
    'find("image.png") -> [x, y] or [], pixel(x, y) -> "#rrggbb", '
    'focus("app", "title") -> true/false, layer("name"), profile("name")'
)

_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_COMPARE = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: _contains(b, a), ast.NotIn: lambda a, b: not _contains(b, a),
}


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, list):
        return item in container
    return to_text(item) in to_text(container)


# ------------------------------------------------------------------ checking
_EXPRESSION_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.Compare, ast.IfExp, ast.Call, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
    ast.Subscript, ast.Slice, ast.JoinedStr, ast.FormattedValue, ast.keyword,
    *_BINARY, *_COMPARE,
)
_STATEMENT_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.If, ast.While, ast.For, ast.Break,
    ast.Continue, ast.Pass, ast.Store,
)


def parse(source: str, mode: str, commands: set[str] = frozenset()) -> ast.AST:
    """Parse and check ``source`` (``mode``: "eval" or "exec"); raises ``ScriptError``."""
    try:
        tree = ast.parse(source.strip() if mode == "eval" else source, mode=mode)
    except SyntaxError as exc:
        where = f" (line {exc.lineno})" if mode == "exec" and exc.lineno else ""
        raise ScriptError(f"syntax error{where}: {exc.msg}") from None
    allowed = _EXPRESSION_NODES + (_STATEMENT_NODES if mode == "exec" else ())
    known = set(FUNCTIONS) | set(commands)
    for node in ast.walk(tree):
        line = f" (line {node.lineno})" if mode == "exec" and hasattr(node, "lineno") else ""
        if not isinstance(node, allowed):
            raise ScriptError(f"{_describe(node)} is not supported{line}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in known):
            name = node.func.id if isinstance(node.func, ast.Name) else "this"
            raise ScriptError(f"unknown function {name}(){line}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ScriptError(f"names cannot start with _{line}")
        if isinstance(node, ast.Assign) and (len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)):
            raise ScriptError(f"only simple assignments like x = 1 are supported{line}")
        if isinstance(node, (ast.AugAssign, ast.For)) and not isinstance(node.target, ast.Name):
            raise ScriptError(f"only a simple name can be assigned{line}")
        if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            raise ScriptError(f"format specifications in f-strings are not supported{line}")
        if isinstance(node, (ast.While, ast.For)) and node.orelse:
            raise ScriptError(f"else after a loop is not supported{line}")
    return tree


def _describe(node: ast.AST) -> str:
    names = {ast.Attribute: "attribute access (x.y)", ast.Import: "import", ast.ImportFrom: "import",
             ast.FunctionDef: "def", ast.Lambda: "lambda", ast.ClassDef: "class", ast.With: "with",
             ast.Try: "try", ast.Dict: "dictionaries", ast.ListComp: "list comprehensions",
             ast.Return: "return", ast.Global: "global", ast.Delete: "del", ast.Raise: "raise"}
    return names.get(type(node), type(node).__name__)


def check_expression(source: str) -> None:
    parse(source, "eval")


def check_script(source: str) -> None:
    from keyboardmanager.core.executor import SCRIPT_COMMANDS

    parse(source, "exec", SCRIPT_COMMANDS)


def names(source: str, mode: str = "eval") -> tuple[set[str], set[str]]:
    """The variables ``source`` reads and those it assigns (``mode`` as for ``parse``);
    two empty sets when it does not parse."""
    from keyboardmanager.core.executor import SCRIPT_COMMANDS

    try:
        tree = parse(source, mode, SCRIPT_COMMANDS if mode == "exec" else frozenset())
    except ScriptError:
        return set(), set()
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    read, assigned = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and id(node) not in called:
            if isinstance(node.ctx, ast.Store):
                assigned.add(node.id)
            elif node.id not in ("true", "false"):
                read.add(node.id)
        if isinstance(node, ast.AugAssign):  # x += 1 reads x first
            read.add(node.target.id)  # type: ignore[attr-defined]
    return read, assigned


# ---------------------------------------------------------------- evaluating
Lookup = Callable[[str], Any]  # name -> value; raises KeyError when unknown
Tick = Callable[[], None]       # called every statement / loop round (stop, pause, limits)


class _Evaluator:
    def __init__(self, lookup: Lookup, commands: dict[str, Callable[..., Any]] | None = None,
                 assign: Callable[[str, Any], None] | None = None, tick: Tick | None = None) -> None:
        self.lookup, self.commands = lookup, commands or {}
        self.assign, self.tick = assign, tick or (lambda: None)
        self.locals: dict[str, Any] = {}

    # expressions
    def ev(self, node: ast.AST) -> Any:
        method = getattr(self, f"_{type(node).__name__}", None)
        if method is None:
            raise ScriptError(f"{_describe(node)} is not supported")
        return method(node)

    def _Expression(self, node):
        return self.ev(node.body)

    def _Constant(self, node):
        return node.value

    def _Name(self, node):
        if node.id in self.locals:
            return self.locals[node.id]
        if node.id in ("true", "false"):
            return node.id == "true"
        try:
            return to_value(self.lookup(node.id))
        except KeyError:
            raise ScriptError(f"unknown variable {node.id}") from None

    def _List(self, node):
        return [self.ev(e) for e in node.elts]

    _Tuple = _List

    def _BoolOp(self, node):
        if isinstance(node.op, ast.And):
            result = True
            for value in node.values:
                result = self.ev(value)
                if not truthy(result):
                    return result
            return result
        result = False
        for value in node.values:
            result = self.ev(value)
            if truthy(result):
                return result
        return result

    def _UnaryOp(self, node):
        value = self.ev(node.operand)
        if isinstance(node.op, ast.Not):
            return not truthy(value)
        number = _number(value)
        return -number if isinstance(node.op, ast.USub) else number

    def _BinOp(self, node):
        left, right = self.ev(node.left), self.ev(node.right)
        op = type(node.op)
        if op is ast.Add and (isinstance(left, str) or isinstance(right, str)):
            result = to_text(left) + to_text(right)  # "n = " + 3 just works
        elif op is ast.Add and isinstance(left, list) and isinstance(right, list):
            result = left + right
        elif op is ast.Mult and isinstance(left, (str, list)) != isinstance(right, (str, list)):
            seq, times = (left, right) if isinstance(left, (str, list)) else (right, left)
            if len(seq) * max(0, int(_number(times))) > MAX_TEXT:
                raise ScriptError("the result would be too long")
            result = seq * int(_number(times))
        else:
            a, b = _number(left), _number(right)
            if op is ast.Pow and abs(b) > 100:
                raise ScriptError("exponent too large")
            try:
                result = _BINARY[op](a, b)
            except ZeroDivisionError:
                raise ScriptError("division by zero") from None
            except OverflowError:
                raise ScriptError("the number is too large") from None
        if isinstance(result, (str, list)) and len(result) > MAX_TEXT:
            raise ScriptError("the result would be too long")
        if (isinstance(result, int) and result.bit_length() > MAX_INT_BITS) or (
                isinstance(result, float) and not math.isfinite(result)):
            raise ScriptError("the number is too large")
        return result

    def _Compare(self, node):
        left = self.ev(node.left)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = self.ev(right_node)
            a, b = left, right
            if type(op) in (ast.Lt, ast.LtE, ast.Gt, ast.GtE):
                if isinstance(a, str) != isinstance(b, str):  # compare "10" with 9 as numbers
                    try:
                        a, b = _number(a), _number(b)
                    except ScriptError:
                        a, b = to_text(a), to_text(b)
            elif type(op) in (ast.Eq, ast.NotEq) and isinstance(a, str) != isinstance(b, str):
                a, b = to_text(a), to_text(b)
            try:
                if not _COMPARE[type(op)](a, b):
                    return False
            except TypeError:
                raise ScriptError(f"cannot compare {to_text(a)!r} and {to_text(b)!r}") from None
            left = right
        return True

    def _IfExp(self, node):
        return self.ev(node.body) if truthy(self.ev(node.test)) else self.ev(node.orelse)

    def _Subscript(self, node):
        value = self.ev(node.value)
        if not isinstance(value, list):
            value = to_text(value)
        if isinstance(node.slice, ast.Slice):
            parts = [None if p is None else int(_number(self.ev(p))) for p in
                     (node.slice.lower, node.slice.upper, node.slice.step)]
            if parts[2] == 0:
                raise ScriptError("the slice step cannot be 0")
            return value[slice(*parts)]
        try:
            return value[int(_number(self.ev(node.slice)))]
        except IndexError:
            raise ScriptError("index out of range") from None

    def _JoinedStr(self, node):
        return "".join(to_text(self.ev(v.value)) if isinstance(v, ast.FormattedValue) else v.value
                       for v in node.values)

    def _Call(self, node):
        name = node.func.id
        args = [self.ev(a) for a in node.args]
        kwargs = {k.arg: self.ev(k.value) for k in node.keywords}
        function = self.commands.get(name) or FUNCTIONS.get(name)
        if function is None:
            raise ScriptError(f"unknown function {name}()")
        try:
            return function(*args, **kwargs)
        except ScriptError:
            raise
        except TypeError as exc:
            raise ScriptError(f"{name}(): {exc}") from None
        except (ValueError, re.error) as exc:
            raise ScriptError(f"{name}(): {exc}") from None

    # statements
    def run(self, body: list[ast.stmt]) -> None:
        for statement in body:
            self.tick()
            try:
                self.statement(statement)
            except ScriptError as exc:
                if "(line " in str(exc):
                    raise
                raise ScriptError(f"{exc} (line {statement.lineno})") from None

    def statement(self, node: ast.stmt) -> None:
        if isinstance(node, ast.Expr):
            self.ev(node.value)
        elif isinstance(node, ast.Assign):
            self._set(node.targets[0].id, self.ev(node.value))
        elif isinstance(node, ast.AugAssign):
            current = self._Name(ast.Name(id=node.target.id))
            fake = ast.BinOp(left=ast.Constant(current), op=node.op, right=node.value)
            self._set(node.target.id, self._BinOp(fake))
        elif isinstance(node, ast.If):
            self.run(node.body if truthy(self.ev(node.test)) else node.orelse)
        elif isinstance(node, ast.While):
            while truthy(self.ev(node.test)):
                self.tick()
                if self._loop_body(node.body):
                    break
        elif isinstance(node, ast.For):
            items = self.ev(node.iter)
            if not isinstance(items, list):
                items = list(to_text(items))
            for item in items:
                self.tick()
                self._set(node.target.id, item)
                if self._loop_body(node.body):
                    break
        elif isinstance(node, ast.Break):
            raise _Break
        elif isinstance(node, ast.Continue):
            raise _Continue
        # ast.Pass: nothing

    def _loop_body(self, body) -> bool:
        """Run a loop body; True when a break left the loop."""
        try:
            self.run(body)
        except _Break:
            return True
        except _Continue:
            pass
        return False

    def _set(self, name: str, value: Any) -> None:
        if name in FUNCTIONS or name in self.commands:
            raise ScriptError(f"{name} is a function and cannot be assigned")
        self.locals[name] = value
        if self.assign is not None:
            self.assign(name, value)


def evaluate(source: str, lookup: Lookup) -> Any:
    """Evaluate an expression (no commands, no side effects)."""
    tree = parse(source, "eval")
    return _Evaluator(lookup).ev(tree)


def run_script(source: str, lookup: Lookup, assign: Callable[[str, Any], None],
               commands: dict[str, Callable[..., Any]], tick: Tick) -> None:
    """Run a script. ``assign`` receives every assignment (to share it with the macro)."""
    tree = parse(source, "exec", set(commands))
    evaluator = _Evaluator(lookup, commands, assign, tick)
    try:
        evaluator.run(tree.body)
    except (_Break, _Continue):
        raise ScriptError("break / continue outside a loop") from None
