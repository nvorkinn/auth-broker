"""A small line-oriented emitter for Python source, plus the formatting helpers the generators share.

It lays code out the way `ruff format` would (double quotes, trailing commas, 120 columns), so generated
files need no post-processing."""

import json
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

WIDTH = 120
INDENT = "    "


class Raw(str):
    """Source text to emit as it is, inside a literal."""


def quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _flat(value: Any) -> str:
    if isinstance(value, Raw):
        return str(value)
    if isinstance(value, str):
        return quote(value)
    if value is None or isinstance(value, bool | int | float):
        return repr(value)
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_flat(k)}: {_flat(v)}" for k, v in value.items()) + "}"
    if isinstance(value, tuple):
        inner = ", ".join(_flat(v) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    if isinstance(value, Sequence):
        return "[" + ", ".join(_flat(v) for v in value) + "]"
    raise TypeError(f"can't write {value!r} as a literal")


def literal(value: Any, indent: int, column: int) -> str:
    """`value` as source. `indent` is the column of the statement it is in and `column` where the literal
    starts on its first line; a value that doesn't fit is broken one element per line."""
    flat = _flat(value)
    if column + len(flat) <= WIDTH or isinstance(value, Raw | str) or not value:
        return flat
    pad = " " * (indent + 4)
    if isinstance(value, Mapping):
        rows = []
        for key, item in value.items():
            head = f"{pad}{_flat(key)}: "
            rows.append(f"{head}{literal(item, indent + 4, len(head))},")
        opening, closing = "{", "}"
    else:
        rows = [f"{pad}{literal(item, indent + 4, len(pad))}," for item in value]
        opening, closing = ("(", ")") if isinstance(value, tuple) else ("[", "]")
    return opening + "\n" + "\n".join(rows) + "\n" + " " * indent + closing


def string_expr(text: str, indent: int, column: int) -> str:
    """A string literal, split into adjacent literals inside parentheses when it would run past the margin."""
    flat = quote(text)
    if column + len(flat) <= WIDTH:
        return flat
    pad = " " * (indent + 4)
    chunks = textwrap.wrap(text, width=WIDTH - len(pad) - 3, drop_whitespace=False, break_long_words=False)
    if len(chunks) < 2:
        return flat
    return "(\n" + "\n".join(f"{pad}{quote(chunk)}" for chunk in chunks) + "\n" + " " * indent + ")"


def docstring_lines(parts: Sequence[str | tuple[str, ...]], indent: int) -> list[str]:
    """A docstring from `parts`: each string is a paragraph, re-wrapped to fit; each tuple is lines to keep
    as they are (an `Args:` list, say)."""
    width = WIDTH - indent - 6
    blocks: list[list[str]] = []
    for part in parts:
        if isinstance(part, tuple):
            blocks.append([_escape(row) for row in part])
        elif part.strip():
            for paragraph in part.strip().split("\n\n"):
                words = _escape(" ".join(paragraph.split()))
                blocks.append(textwrap.wrap(words, width=min(100, width), break_long_words=False) or [""])
    rows = [row for index, block in enumerate(blocks) for row in ([""] if index else []) + block]
    if len(rows) == 1 and len(rows[0]) <= width:
        return [f'"""{rows[0]}"""']
    return ['"""' + rows[0], *rows[1:], '"""']


def _escape(text: str) -> str:
    text = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return text[:-1] + '\\"' if text.endswith('"') else text


def import_lines(module: str, names: Iterable[str]) -> list[str]:
    ordered = sorted(set(names), key=_import_key)
    one_line = f"from {module} import {', '.join(ordered)}"
    if len(one_line) <= WIDTH:
        return [one_line]
    return [f"from {module} import (", *(f"{INDENT}{name}," for name in ordered), ")"]


def _import_key(name: str) -> tuple[int, str]:
    # isort's default order: CONSTANTS, then Classes, then functions and variables.
    if len(name) > 1 and name.isupper():
        return (0, name)
    return (1 if name[:1].isupper() else 2, name)


class CodeWriter:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self.level = 0

    @property
    def column(self) -> int:
        return self.level * len(INDENT)

    def line(self, text: str = "") -> None:
        self._lines.append(f"{INDENT * self.level}{text}" if text else "")

    def raw(self, text: str) -> None:
        """Lines whose indentation is already absolute (what `literal` and `string_expr` return)."""
        self._lines.extend(text.split("\n"))

    def blank(self, count: int = 1) -> None:
        if not self._lines:
            return
        trailing = 0
        for existing in reversed(self._lines):
            if existing:
                break
            trailing += 1
        self._lines.extend([""] * max(0, count - trailing))

    def statement(self, prefix: str, value: Any) -> None:
        """`prefix` followed by `value` as a literal, e.g. `statement("_X = ", {...})`."""
        self.raw(f"{INDENT * self.level}{prefix}{literal(value, self.column, self.column + len(prefix))}")

    def docstring(self, *parts: str | tuple[str, ...]) -> None:
        for row in docstring_lines(parts, self.column):
            self.line(row)

    def block(self, header: str | None = None) -> "_Block":
        """`with w.block("class A:"):` writes the header and indents what follows (no header: only indents)."""
        if header is not None:
            self.line(header)
        return _Block(self)

    def getvalue(self) -> str:
        while self._lines and not self._lines[-1]:
            self._lines.pop()
        return "\n".join(self._lines) + "\n"


class _Block:
    def __init__(self, writer: CodeWriter) -> None:
        self._writer = writer

    def __enter__(self) -> None:
        self._writer.level += 1

    def __exit__(self, *exc: object) -> None:
        self._writer.level -= 1
