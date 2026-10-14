"""Writing `clients/<route>.py`: a class per route with one method per HTTP verb."""

import textwrap

from .emit_models import HEADER, stdlib_import_lines
from .operations import Operation, Param, Route
from .writer import WIDTH, CodeWriter, Raw, import_lines, quote


def render_client(route: Route, model_names: set[str], source: str) -> str:
    deps: set[str] = set()
    symbols: set[str] = set()
    for operation in route.operations:
        deps |= operation.type_deps()
        symbols |= operation.type_imports(server=False)

    w = CodeWriter()
    w.line(HEADER.format(source=source))
    w.line(f'"""Client for ``{route.path}``."""')
    w.blank()
    w.line("from __future__ import annotations")
    stdlib = stdlib_import_lines(symbols)
    if stdlib:
        w.blank()
        for line in stdlib:
            w.line(line)
    w.blank()
    local = sorted(deps & model_names)
    if local:
        for line in import_lines("..models", local):
            w.line(line)
    for line in import_lines("._base", ["BaseClient"]):
        w.line(line)

    for operation in route.operations:
        w.blank()
        constant = operation.method.upper()
        w.statement(f"_{constant}_RESPONSES = ", {r.status: Raw(r.expr) for r in operation.responses})
        if operation.security:
            w.blank()
            w.statement(f"_{constant}_SECURITY = ", operation.security)

    w.blank(2)
    with w.block(f"class {route.prefix}Client(BaseClient):"):
        w.docstring(f"Calls for ``{route.path}``.")
        for operation in route.operations:
            w.blank()
            _render_method(w, route, operation)
    return w.getvalue()


def _render_method(w: CodeWriter, route: Route, operation: Operation) -> None:
    path_params = [p for p in operation.params if p.location == "path"]
    others = [p for p in operation.params if p.location != "path"]
    body = operation.body

    arguments = ["self", *(f"{p.name}: {p.plain.expr}" for p in path_params)]
    if body and body.required:
        arguments.append(f"body: {body.plain.expr}")
    keyword = [f"{p.name}: {p.plain.expr}" for p in others if p.required]
    keyword += [f"{p.name}: {p.plain.with_none().expr} = None" for p in others if not p.required]
    if body and not body.required:
        keyword.append(f"body: {body.plain.with_none().expr} = None")
    if keyword:
        arguments += ["*", *keyword]

    name, returns = operation.method, operation.return_expr
    header = f"def {name}({', '.join(arguments)}) -> {returns}:"
    if w.column + len(header) <= WIDTH:
        w.line(header)
    else:
        w.line(f"def {name}(")
        for argument in arguments:
            w.line(f"    {argument},")
        w.line(f") -> {returns}:")

    with w.block():
        w.docstring(*_docstring_parts(route, operation))
        constant = operation.method.upper()
        sent = [f'"{constant}",', f'"{route.client_path}",']
        for keyword_name, location in (
            ("path_params", "path"),
            ("query", "query"),
            ("headers", "header"),
            ("cookies", "cookie"),
        ):
            entries = [p for p in operation.params if p.location == location]
            if entries:
                sent.append(f"{keyword_name}={_dict_source(entries, location)},")
        if body:
            sent.append("body=body,")
        sent.append(f"responses=_{constant}_RESPONSES,")
        if operation.security:
            sent.append(f"security=_{constant}_SECURITY,")
        w.line("return self._send(")
        for row in sent:
            w.line(f"    {row}")
        w.line(")")


def _dict_source(params: list[Param], location: str) -> str:
    return "{" + ", ".join(f"{quote(p.name if location == 'path' else p.wire)}: {p.name}" for p in params) + "}"


def _docstring_parts(route: Route, operation: Operation) -> list[str | tuple[str, ...]]:
    parts: list[str | tuple[str, ...]] = [operation.summary or f"{operation.method.upper()} {route.path}."]
    if operation.description:
        parts.append(operation.description)
    if operation.deprecated:
        parts.append("Deprecated.")
    described = [(p.name, p.description) for p in operation.params if p.description]
    if operation.body and operation.body.description:
        described.append(("body", operation.body.description))
    if described:
        rows = ["Args:"]
        for name, text in described:
            rows += textwrap.wrap(
                f"{name}: {' '.join(text.split())}", width=88, initial_indent="    ", subsequent_indent="        "
            )
        parts.append(tuple(rows))
    errors = [str(r.status) for r in operation.responses if not r.is_success]
    parts.append("Raises ApiError for an error status" + (f" ({', '.join(errors)})." if errors else "."))
    return parts
