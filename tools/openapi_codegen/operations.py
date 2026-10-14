"""Reading paths, operations, parameters, bodies, responses and security out of an OpenAPI document."""

import re
from dataclasses import dataclass, field
from typing import Any

from .naming import Names, parameter_name, pascal, snake
from .schema import Schemas, SpecError, TypeRef, warn

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
LOCATIONS = ("path", "query", "header", "cookie")
RESERVED_PARAMETER_NAMES = frozenset({"self", "body"})
_PATH_VARIABLE = re.compile(r"\{([^}]+)\}")


@dataclass
class Param:
    wire: str
    name: str
    location: str
    required: bool
    type: TypeRef  # with constraints, for validating on the server
    plain: TypeRef  # without, for a client's signature
    description: str = ""

    @property
    def is_array(self) -> bool:
        return self.type.expr.removeprefix("Annotated[").startswith("list[")


@dataclass
class Body:
    type: TypeRef
    plain: TypeRef
    required: bool
    description: str = ""


@dataclass
class ResponseDef:
    status: int | str  # 200, "2XX" or "default"
    expr: str  # "ConfigResponse", "list[Item]", "None" for no content, "str", "bytes"
    deps: frozenset[str] = frozenset()
    imports: frozenset[str] = frozenset()
    description: str = ""

    @property
    def is_success(self) -> bool:
        return self.status == "2XX" or (isinstance(self.status, int) and 200 <= self.status < 300)


@dataclass
class Operation:
    method: str
    path: str
    summary: str
    description: str
    params: list[Param]
    body: Body | None
    responses: list[ResponseDef]
    security: list[dict[str, dict[str, Any]]]
    deprecated: bool = False

    @property
    def success(self) -> list[ResponseDef]:
        return [r for r in self.responses if r.is_success]

    @property
    def default_status(self) -> int:
        codes = sorted(r.status for r in self.success if isinstance(r.status, int))
        return codes[0] if codes else 200

    @property
    def return_expr(self) -> str:
        """The annotation for what a successful call produces."""
        members: list[str] = []
        for response in self.success:
            if response.expr not in members:
                members.append(response.expr)
        if not members:
            return "Any"
        ordered = [m for m in members if m != "None"] + (["None"] if "None" in members else [])
        return " | ".join(ordered)

    def type_deps(self) -> set[str]:
        found: set[str] = set()
        for param in self.params:
            found |= param.type.deps
        if self.body:
            found |= self.body.type.deps
        for response in self.responses:
            found |= response.deps
        return found

    def type_imports(self, server: bool) -> set[str]:
        """The typing/stdlib/pydantic symbols the types in this operation need: with their constraints on a
        server (which validates), without in a client's signature."""
        found: set[str] = set()
        for param in self.params:
            found |= (param.type if server else param.plain).imports
        if self.body:
            found |= (self.body.type if server else self.body.plain).imports
        for response in self.responses:
            found |= response.imports
        if not self.success:
            found.add("Any")
        return found


@dataclass
class Route:
    path: str
    module: str
    prefix: str  # PascalCase stem for the class names
    operations: list[Operation] = field(default_factory=list)

    @property
    def flask_rule(self) -> str:
        def convert(match: re.Match[str]) -> str:
            param = next(
                (p for p in self.operations[0].params if p.location == "path" and p.wire == match.group(1)), None
            )
            converter = _flask_converter(param)
            return f"<{converter}{param.name if param else parameter_name(match.group(1))}>"

        return _PATH_VARIABLE.sub(convert, self.path)

    @property
    def client_path(self) -> str:
        def convert(match: re.Match[str]) -> str:
            param = next(
                (p for p in self.operations[0].params if p.location == "path" and p.wire == match.group(1)), None
            )
            return "{" + (param.name if param else parameter_name(match.group(1))) + "}"

        return _PATH_VARIABLE.sub(convert, self.path)


def _flask_converter(param: Param | None) -> str:
    if param is None:
        return ""
    expr = param.plain.expr
    if expr == "UUID":
        return "uuid:"
    return {"int": "int:", "float": "float:"}.get(expr, "")


def _is_json(media_type: str) -> bool:
    base = media_type.split(";")[0].strip().lower()
    return base == "application/json" or base.endswith("+json")


class Operations:
    def __init__(self, schemas: Schemas) -> None:
        self.schemas = schemas
        self.document = schemas.document

    def routes(self) -> list[Route]:
        routes: list[Route] = []
        names = Names()
        for path, item in self.document.get("paths", {}).items():
            item = self.schemas.resolve(item)
            stem = _path_stem(path)
            module = names.reserve(snake(stem) or "root")
            prefix = pascal(module)
            route = Route(path, module, prefix)
            shared = item.get("parameters", [])
            for method in METHODS:
                if method in item:
                    route.operations.append(self._operation(route, method, item[method], shared))
            if route.operations:
                self._check_path_parameters(route)
                routes.append(route)
        return routes

    # -- one operation --------------------------------------------------------------------------------

    def _operation(self, route: Route, method: str, node: dict[str, Any], shared: list[Any]) -> Operation:
        hint = f"{route.prefix}{method.capitalize()}"
        if node.get("operationId"):
            hint = pascal(node["operationId"])
        params = self._params(route, hint, [*shared, *node.get("parameters", [])])
        return Operation(
            method=method,
            path=route.path,
            summary=node.get("summary", ""),
            description=node.get("description", ""),
            params=params,
            body=self._body(route, method, hint, node.get("requestBody")),
            responses=self._responses(hint, node.get("responses", {})),
            security=self._security(node.get("security", self.document.get("security", []))),
            deprecated=bool(node.get("deprecated")),
        )

    def _params(self, route: Route, hint: str, raw: list[Any]) -> list[Param]:
        # An operation's own parameter replaces a path-level one with the same name and location.
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in raw:
            entry = self.schemas.resolve(entry)
            location = entry.get("in")
            if location not in LOCATIONS:
                raise SpecError(f"parameter {entry.get('name')!r} has unknown location {location!r}")
            merged[(entry["name"], location)] = entry

        params: list[Param] = []
        taken: set[str] = set(RESERVED_PARAMETER_NAMES)
        # Path parameters first, so they claim their names ahead of a query parameter of the same name.
        for (wire, location), entry in sorted(merged.items(), key=lambda kv: LOCATIONS.index(kv[0][1])):
            name = parameter_name(wire, taken)
            taken.add(name)
            schema = entry.get("schema", {"type": "string"})
            if "content" in entry:
                warn(f"parameter {wire!r}: content-encoded parameters are read as plain strings")
                schema = {"type": "string"}
            sub_hint = f"{hint}{pascal(wire)}"
            params.append(
                Param(
                    wire=wire,
                    name=name,
                    location=location,
                    required=bool(entry.get("required")) or location == "path",
                    type=self.schemas.type_of(schema, sub_hint),
                    plain=self.schemas.type_of(schema, sub_hint, constraints=False),
                    description=entry.get("description", ""),
                )
            )
        return params

    def _body(self, route: Route, method: str, hint: str, node: Any) -> Body | None:
        if node is None:
            return None
        node = self.schemas.resolve(node)
        content = node.get("content", {})
        media = next((key for key in content if _is_json(key)), None)
        if media is None:
            if content:
                warn(f"{method.upper()} {route.path}: request body of {', '.join(content)} isn't supported; skipped")
            return None
        schema = content[media].get("schema", {})
        return Body(
            self.schemas.type_of(schema, f"{hint}Request"),
            self.schemas.type_of(schema, f"{hint}Request", constraints=False),
            bool(node.get("required")),
            node.get("description", ""),
        )

    def _responses(self, hint: str, raw: dict[str, Any]) -> list[ResponseDef]:
        found: list[ResponseDef] = []
        for key, node in raw.items():
            node = self.schemas.resolve(node)
            status: int | str = int(key) if key.isdigit() else key.upper() if key.upper().endswith("XX") else "default"
            content = node.get("content", {})
            media = next((m for m in content if _is_json(m)), None)
            suffix = "Response" if status in (200, "2XX") or len(raw) == 1 else f"Response{key}"
            if media is not None:
                ref = self.schemas.type_of(content[media].get("schema", {}), f"{hint}{suffix}")
                response = ResponseDef(status, ref.expr, ref.deps, ref.imports, node.get("description", ""))
            elif content:
                text = all(m.startswith("text/") for m in content)
                response = ResponseDef(status, "str" if text else "bytes", description=node.get("description", ""))
            else:
                response = ResponseDef(status, "None", description=node.get("description", ""))
            found.append(response)
        return found

    def _security(self, requirements: list[dict[str, list[str]]]) -> list[dict[str, dict[str, Any]]]:
        schemes = self.document.get("components", {}).get("securitySchemes", {})
        resolved = []
        for requirement in requirements:
            alternative: dict[str, dict[str, Any]] = {}
            for name, scopes in requirement.items():
                if name not in schemes:
                    raise SpecError(f"security requirement names an undefined scheme {name!r}")
                scheme = self.schemas.resolve(schemes[name])
                info: dict[str, Any] = {"type": scheme.get("type", "")}
                for key in ("scheme", "in", "name"):
                    if key in scheme:
                        info[key] = scheme[key]
                info["scopes"] = list(scopes)
                alternative[name] = info
            resolved.append(alternative)
        return resolved

    def _check_path_parameters(self, route: Route) -> None:
        declared = set(_PATH_VARIABLE.findall(route.path))
        for operation in route.operations:
            documented = {p.wire for p in operation.params if p.location == "path"}
            if declared != documented:
                raise SpecError(
                    f"{operation.method.upper()} {route.path}: path parameters {sorted(documented)} "
                    f"don't match the {sorted(declared)} in the path"
                )
        # Every operation on a route shares one URL rule, so they must agree on the names Flask will see.
        names = {frozenset((p.wire, p.name) for p in op.params if p.location == "path") for op in route.operations}
        if len(names) > 1:
            raise SpecError(f"{route.path}: operations name their path parameters differently")


def _path_stem(path: str) -> str:
    parts = []
    for segment in path.strip("/").split("/"):
        match = _PATH_VARIABLE.fullmatch(segment)
        parts.append(f"by_{snake(match.group(1))}" if match else segment)
    return "_".join(p for p in parts if p)
