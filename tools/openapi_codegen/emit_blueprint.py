"""Writing `blueprints/<route>.py`: a view class per route and a function that builds its Flask blueprint.

The view has one method per HTTP verb that does the plumbing (authenticate, read and validate the request,
build the response) and calls an overridable `handle_<verb>` method with the validated values. A server
subclasses the view and implements the handlers; the generated file is never edited."""

from .emit_models import HEADER, render_field, stdlib_import_lines
from .operations import Operation, Param, Route
from .schema import FieldDef
from .writer import WIDTH, CodeWriter, Raw, import_lines, quote


def render_blueprint(route: Route, model_names: set[str], source: str) -> str:
    deps: set[str] = set()
    symbols: set[str] = set()
    for operation in route.operations:
        deps |= operation.type_deps()
        symbols |= operation.type_imports(server=True)
        if any(p.location == "path" for p in operation.params):
            symbols.add("Any")
    has_params = any(op.params for op in route.operations)
    pydantic: set[str] = set()
    if has_params:
        pydantic |= {"BaseModel", "ConfigDict"}
    if "Field" in symbols or any(p.name != p.wire or p.description for op in route.operations for p in op.params):
        pydantic.add("Field")
    base_names = {"ApiHTTPError", "BaseView"} | ({"ParamSpec"} if has_params else set())

    w = CodeWriter()
    w.line(HEADER.format(source=source))
    w.line(f'"""Flask view and blueprint for ``{route.path}``."""')
    w.blank()
    w.line("from __future__ import annotations")
    stdlib = stdlib_import_lines(symbols)
    if stdlib:
        w.blank()
        for line in stdlib:
            w.line(line)
    w.blank()
    for line in import_lines("flask", ["Blueprint", "Response"]):
        w.line(line)
    if pydantic:
        for line in import_lines("pydantic", pydantic):
            w.line(line)
    w.blank()
    local = sorted(deps & model_names)
    if local:
        for line in import_lines("..models", local):
            w.line(line)
    for line in import_lines("._base", base_names):
        w.line(line)

    for operation in route.operations:
        constant = operation.method.upper()
        if operation.params:
            w.blank(2)
            _render_params_model(w, operation)
            w.blank(2)
            w.statement(f"_{constant}_PARAMS = ", tuple(Raw(_param_spec(p)) for p in operation.params))
        w.blank()
        w.statement(f"_{constant}_RESPONSES = ", {r.status: Raw(r.expr) for r in operation.responses})
        w.blank()
        w.statement(f"_{constant}_SECURITY = ", operation.security)

    w.blank(2)
    with w.block(f"class {route.prefix}View(BaseView):"):
        w.docstring(
            f"Serves ``{route.path}``. Subclass it, implement the `handle_*` methods you need and pass the "
            "subclass to `create_blueprint`; a verb left unimplemented answers 501."
        )
        for operation in route.operations:
            w.blank()
            _render_verb(w, route, operation)
            w.blank()
            _render_handler(w, route, operation)

    w.blank(2)
    view = f"{route.prefix}View"
    w.line("def create_blueprint(")
    w.line(f"    view: type[{view}] = {view},")
    w.line("    *,")
    w.line("    name: str | None = None,")
    w.line("    url_prefix: str | None = None,")
    w.line(") -> Blueprint:")
    with w.block():
        w.docstring(f"The blueprint for ``{route.path}``, served by `view` (``{view}`` or a subclass of it).")
        w.line(f"blueprint = Blueprint(name or {quote(route.module)}, __name__, url_prefix=url_prefix)")
        w.line(f"blueprint.add_url_rule({quote(route.flask_rule)}, view_func=view.as_view({quote(route.module)}))")
        w.line("return blueprint")
    return w.getvalue()


def _param_spec(param: Param) -> str:
    arguments = [quote(param.wire), quote(param.location)]
    if param.is_array:
        arguments.append("multiple=True")
    if param.location == "path":
        arguments.append(f"key={quote(param.name)}")
    return f"ParamSpec({', '.join(arguments)})"


def _render_params_model(w: CodeWriter, operation: Operation) -> None:
    with w.block(f"class _{operation.method.capitalize()}Params(BaseModel):"):
        w.line("model_config = ConfigDict(populate_by_name=True, protected_namespaces=())")
        w.blank()
        for param in operation.params:
            kind = param.type if param.required else param.type.with_none()
            render_field(w, FieldDef(param.name, param.wire, kind, param.required, None, param.description))


def _handler_signature(operation: Operation) -> list[str]:
    body = operation.body
    required = [f"{p.name}: {p.plain.expr}" for p in operation.params if p.required]
    optional = [f"{p.name}: {p.plain.with_none().expr} = None" for p in operation.params if not p.required]
    if body:
        entry = f"body: {body.plain.expr}" if body.required else f"body: {body.plain.with_none().expr} = None"
        (required if body.required else optional).append(entry)
    return ["self", "*", *required, *optional] if len(required + optional) else ["self"]


def _write_signature(w: CodeWriter, name: str, arguments: list[str], returns: str) -> None:
    header = f"def {name}({', '.join(arguments)}) -> {returns}:"
    if w.column + len(header) <= WIDTH:
        w.line(header)
        return
    w.line(f"def {name}(")
    for argument in arguments:
        w.line(f"    {argument},")
    w.line(f") -> {returns}:")


def _render_verb(w: CodeWriter, route: Route, operation: Operation) -> None:
    constant = operation.method.upper()
    has_path = any(p.location == "path" for p in operation.params)
    arguments = ["self", "**path_values: Any"] if has_path else ["self"]
    _write_signature(w, operation.method, arguments, "Response")
    with w.block():
        w.docstring(f"Handles ``{constant} {route.path}``: checks the request, calls `handle_{operation.method}`.")
        with w.block("try:"):
            w.line(f"self.authenticate(_{constant}_SECURITY)")
            if operation.params:
                path_values = "path_values" if has_path else "{}"
                params_model = f"_{operation.method.capitalize()}Params"
                w.line(f"params = self.read_params({params_model}, _{constant}_PARAMS, {path_values})")
            if operation.body:
                w.line(f"body = self.read_body({operation.body.type.expr}, required={operation.body.required})")
            passed = [f"{p.name}=params.{p.name}" for p in operation.params]
            if operation.body:
                passed.append("body=body")
            w.line(f"result = self.handle_{operation.method}({', '.join(passed)})")
        with w.block("except ApiHTTPError as error:"):
            w.line("return self.error_response(error)")
        w.line(f"return self.respond(result, _{constant}_RESPONSES, {operation.default_status})")


def _render_handler(w: CodeWriter, route: Route, operation: Operation) -> None:
    _write_signature(w, f"handle_{operation.method}", _handler_signature(operation), operation.return_expr)
    with w.block():
        summary = operation.summary or f"{operation.method.upper()} {route.path}."
        parts: list[str | tuple[str, ...]] = [summary]
        if operation.description:
            parts.append(operation.description)
        parts.append(
            f"Override this. It answers {operation.default_status} with what it returns; return `(result, status)` "
            "for another status, or raise `ApiHTTPError` to send an error."
        )
        w.docstring(*parts)
        w.line(f'raise ApiHTTPError(501, message="{operation.method.upper()} {route.path} is not implemented")')


def render_blueprints_init(routes: list[Route], source: str) -> str:
    modules = sorted(route.module for route in routes)
    w = CodeWriter()
    w.line(HEADER.format(source=source))
    w.line('"""Flask views and blueprints, one module per route."""')
    w.blank()
    w.line("from __future__ import annotations")
    w.blank()
    w.line("from collections.abc import Callable")
    w.blank()
    w.line("from flask import Blueprint, Flask")
    w.blank()
    for line in import_lines(".", modules):
        w.line(line)
    for line in import_lines("._base", ["ApiHTTPError", "BaseView"]):
        w.line(line)
    w.blank()
    w.line("__all__ = [")
    for name in ["ApiHTTPError", "BaseView", "register_all"]:
        w.line(f'    "{name}",')
    w.line("]")
    w.blank()
    w.line("_ROUTES: list[tuple[type[BaseView], Callable[..., Blueprint]]] = [")
    for route in sorted(routes, key=lambda r: r.module):
        w.line(f"    ({route.module}.{route.prefix}View, {route.module}.create_blueprint),")
    w.line("]")
    w.blank(2)
    w.line("def register_all(app: Flask, *views: type[BaseView], url_prefix: str | None = None) -> None:")
    with w.block():
        w.docstring(
            "Registers a blueprint for every route. Pass your subclasses of the generated views as `views`; a route "
            "with none is served by its generated view, which answers 501."
        )
        w.line("remaining = list(views)")
        w.line("for base, create in _ROUTES:")
        with w.block():
            w.line("mine = [view for view in remaining if issubclass(view, base)]")
            w.line("if len(mine) > 1:")
            w.line('    raise ValueError(f"more than one view for {base.__name__}: {mine}")')
            w.line("remaining = [view for view in remaining if view not in mine]")
            w.line("app.register_blueprint(create(mine[0] if mine else base, url_prefix=url_prefix))")
        w.line("if remaining:")
        w.line('    raise ValueError(f"not a subclass of any generated view: {remaining}")')
    return w.getvalue()
