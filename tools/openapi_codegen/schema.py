"""Reading JSON Schema out of an OpenAPI document into Python types and Pydantic model definitions.

`Schemas.type_of` turns any schema into a `TypeRef` (a Python type expression plus what it needs), registering
a model for every object it meets along the way. `Schemas.finish` returns them all."""

import sys
from dataclasses import dataclass, field
from typing import Any

from .naming import Names, class_name, field_name, pascal
from .writer import quote

COMPONENT_PREFIX = "#/components/schemas/"

_FORMAT_TYPES = {
    "date-time": ("datetime", "datetime"),
    "date": ("date", "date"),
    "time": ("time", "time"),
    "uuid": ("UUID", "UUID"),
}
_PRIMITIVES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
_SCALAR_DEFAULTS = (str, int, float, bool)


@dataclass(frozen=True)
class TypeRef:
    """A Python type expression and what it needs: the generated classes it names (`deps`) and the
    symbols it takes from typing, datetime, uuid and pydantic (`imports`)."""

    expr: str
    deps: frozenset[str] = frozenset()
    imports: frozenset[str] = frozenset()
    nullable: bool = False

    def with_none(self) -> "TypeRef":
        if self.nullable:
            return self
        return TypeRef(f"{self.expr} | None", self.deps, self.imports, True)


def union(refs: list[TypeRef]) -> TypeRef:
    members: list[str] = []
    nullable = False
    for ref in refs:
        for member in _members(ref):
            if member == "None":
                nullable = True
            elif member not in members:
                members.append(member)
    if nullable:
        members.append("None")
    return TypeRef(
        " | ".join(members) or "Any",
        frozenset().union(*(r.deps for r in refs)),
        frozenset().union(*(r.imports for r in refs)) | ({"Any"} if not members else set()),
        nullable,
    )


def _members(ref: TypeRef) -> list[str]:
    """The top-level members of a union expression (`A | list[B | C] | None` -> `A`, `list[B | C]`, `None`)."""
    members, depth, start = [], 0, 0
    for index, char in enumerate(ref.expr):
        depth += char in "[("
        depth -= char in "])"
        if char == "|" and depth == 0:
            members.append(ref.expr[start:index].strip())
            start = index + 1
    members.append(ref.expr[start:].strip())
    return members


@dataclass
class FieldDef:
    name: str
    wire: str
    type: TypeRef
    required: bool
    default: str | None = None
    description: str = ""

    @property
    def aliased(self) -> bool:
        return self.name != self.wire


@dataclass
class ModelDef:
    name: str
    description: str = ""
    bases: list[str] = field(default_factory=list)
    fields: list[FieldDef] = field(default_factory=list)
    extra: str | None = None

    @property
    def deps(self) -> set[str]:
        found = set(self.bases)
        for item in self.fields:
            found |= item.type.deps
        return found

    @property
    def imports(self) -> set[str]:
        found: set[str] = set()
        for item in self.fields:
            found |= item.type.imports
        return found


@dataclass
class AliasDef:
    name: str
    type: TypeRef
    description: str = ""


class SpecError(ValueError):
    pass


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


class Schemas:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self._names = Names()
        self._component_names: dict[str, str] = {}
        self._inline_names: dict[int, str] = {}
        self._models: dict[str, ModelDef] = {}
        self._aliases: dict[str, AliasDef] = {}
        self._pending: dict[str, Any] = {}
        components = document.get("components", {}).get("schemas", {})
        for key in components:
            self._component_names[COMPONENT_PREFIX + key] = self._names.reserve(class_name(key))
        for key, schema in components.items():
            self._define(self._component_names[COMPONENT_PREFIX + key], schema)

    # -- references -----------------------------------------------------------------------------------

    def resolve(self, node: Any) -> Any:
        """Follows local `$ref`s (to schemas, parameters, responses, request bodies...) until a real node."""
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                raise SpecError(f"only local $refs are supported, not {ref!r}")
            if ref in seen:
                raise SpecError(f"$ref cycle through {ref!r}")
            seen.add(ref)
            target: Any = self.document
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                try:
                    target = target[part]
                except (KeyError, TypeError):
                    raise SpecError(f"unresolvable $ref {ref!r}") from None
            node = target
        return node

    # -- types ----------------------------------------------------------------------------------------

    def type_of(self, schema: Any, hint: str, constraints: bool = True) -> TypeRef:
        """The Python type for `schema`. Inline objects become models named after `hint`. With `constraints`
        false, length/range limits are left out (a client's signature has no use for them)."""
        if schema is True or schema == {} or schema is None:
            return TypeRef("Any", imports=frozenset({"Any"}))
        if schema is False:
            raise SpecError("a `false` schema can't be given a type")
        if "$ref" in schema:
            ref = schema["$ref"]
            if ref in self._component_names:
                name = self._component_names[ref]
                return TypeRef(name, deps=frozenset({name}))
            return self.type_of(self.resolve(schema), hint, constraints)

        result = self._base_type(schema, hint, constraints)
        if schema.get("nullable") is True:
            result = result.with_none()
        return result

    def _base_type(self, schema: dict[str, Any], hint: str, constraints: bool) -> TypeRef:
        if "const" in schema:
            return _literal([schema["const"]])
        if "enum" in schema:
            return _literal(schema["enum"])
        for keyword_ in ("anyOf", "oneOf"):
            if keyword_ in schema:
                options = [
                    self.type_of(option, f"{hint}Option{index}" if len(schema[keyword_]) > 1 else hint, constraints)
                    for index, option in enumerate(schema[keyword_], 1)
                ]
                return union(options)
        kind = schema.get("type")
        if isinstance(kind, list):
            return union([self._typed({**schema, "type": one}, hint, constraints) for one in kind])
        if "allOf" in schema and kind is None:
            members = schema["allOf"]
            if len(members) == 1 and not self._is_object(schema):
                return self.type_of(members[0], hint, constraints)
            return self._inline_model(schema, hint)
        return self._typed(schema, hint, constraints)

    def _typed(self, schema: dict[str, Any], hint: str, constraints: bool) -> TypeRef:
        kind = schema.get("type")
        if kind is None and (schema.get("properties") or "allOf" in schema):
            kind = "object"
        if kind == "null":
            return TypeRef("None", nullable=True)
        if kind == "array":
            if "prefixItems" in schema:
                items = [
                    self.type_of(item, f"{hint}Item{i}", constraints) for i, item in enumerate(schema["prefixItems"], 1)
                ]
                return TypeRef(
                    f"tuple[{', '.join(i.expr for i in items)}]",
                    frozenset().union(*(i.deps for i in items)),
                    frozenset().union(*(i.imports for i in items)),
                )
            inner = self.type_of(schema.get("items", {}), f"{hint}Item", constraints)
            return self._constrained(TypeRef(f"list[{inner.expr}]", inner.deps, inner.imports), schema, constraints)
        if kind == "object" or (kind is None and "additionalProperties" in schema):
            if self._is_object(schema):
                return self._inline_model(schema, hint)
            extra = schema.get("additionalProperties")
            value = self.type_of(extra if isinstance(extra, dict) else {}, f"{hint}Value", constraints)
            return TypeRef(f"dict[str, {value.expr}]", value.deps, value.imports)
        if kind == "string":
            special = _FORMAT_TYPES.get(schema.get("format", ""))
            if special:
                return self._constrained(TypeRef(special[0], imports=frozenset({special[1]})), schema, constraints)
            if schema.get("format") == "binary":
                return TypeRef("bytes")
        if kind in _PRIMITIVES:
            return self._constrained(TypeRef(_PRIMITIVES[kind]), schema, constraints)
        if kind is not None:
            raise SpecError(f"unknown schema type {kind!r}")
        return TypeRef("Any", imports=frozenset({"Any"}))

    @staticmethod
    def _constrained(ref: TypeRef, schema: dict[str, Any], enabled: bool) -> TypeRef:
        if not enabled:
            return ref
        limits: dict[str, Any] = {}
        is_array = ref.expr.startswith("list[")
        for source, target in (
            ("minimum", "ge"),
            ("maximum", "le"),
            ("exclusiveMinimum", "gt"),
            ("exclusiveMaximum", "lt"),
            ("multipleOf", "multiple_of"),
            ("minLength", "min_length"),
            ("maxLength", "max_length"),
            ("pattern", "pattern"),
            ("minItems", "min_length"),
            ("maxItems", "max_length"),
        ):
            value = schema.get(source)
            if value is None or isinstance(value, bool):
                continue
            if source.endswith("Items") != is_array and target in ("min_length", "max_length"):
                continue
            limits[target] = value
        # OpenAPI 3.0 spells exclusive bounds as booleans next to minimum/maximum.
        if schema.get("exclusiveMinimum") is True and "ge" in limits:
            limits["gt"] = limits.pop("ge")
        if schema.get("exclusiveMaximum") is True and "le" in limits:
            limits["lt"] = limits.pop("le")
        if not limits:
            return ref
        arguments = ", ".join(f"{key}={quote(v) if isinstance(v, str) else v!r}" for key, v in limits.items())
        return TypeRef(
            f"Annotated[{ref.expr}, Field({arguments})]", ref.deps, ref.imports | {"Annotated", "Field"}, ref.nullable
        )

    # -- models ---------------------------------------------------------------------------------------

    def _is_object(self, schema: dict[str, Any]) -> bool:
        """Whether `schema` describes a fixed set of named properties (so: a model, not a dict)."""
        if "$ref" in schema:
            return self._is_object(self.resolve(schema))
        if schema.get("properties"):
            return True
        return any(self._is_object(member) for member in schema.get("allOf", ()))

    def _inline_model(self, schema: dict[str, Any], hint: str) -> TypeRef:
        key = id(schema)
        if key not in self._inline_names:
            name = self._names.reserve(class_name(hint))
            self._inline_names[key] = name
            self._define(name, schema)
        name = self._inline_names[key]
        return TypeRef(name, deps=frozenset({name}))

    def _define(self, name: str, schema: Any) -> None:
        """Registers `name` as a model if `schema` is an object, else as a plain alias of its type."""
        is_model = (
            isinstance(schema, dict)
            and not {"$ref", "enum", "const", "anyOf", "oneOf"} & schema.keys()
            and self._is_object(schema)
        )
        if is_model:
            self._models[name] = self._build_model(name, schema)
        else:
            description = schema.get("description", "") if isinstance(schema, dict) else ""
            self._aliases[name] = AliasDef(name, self.type_of(schema, f"{name}Value"), description)

    def _build_model(self, name: str, schema: dict[str, Any]) -> ModelDef:
        model = ModelDef(name, schema.get("description", ""))
        properties: dict[str, Any] = {}
        required: list[str] = []
        parts = [*schema.get("allOf", []), schema]
        for part in parts:
            if part is not schema and "$ref" in part:
                target = part["$ref"]
                if target in self._component_names and self._is_object(self.resolve(part)):
                    model.bases.append(self._component_names[target])
                    continue
                part = self.resolve(part)
            if part is not schema and part.get("allOf"):
                warn(f"{name}: nested allOf; its properties are merged in")
            properties.update(part.get("properties", {}))
            required.extend(part.get("required", []))
            if part.get("additionalProperties") is False:
                model.extra = "forbid"
            elif part.get("additionalProperties") not in (None, False) and model.extra is None:
                model.extra = "allow"

        taken: set[str] = set()
        for wire, sub in properties.items():
            attribute, aliased = field_name(wire)
            while attribute in taken:
                attribute += "_"
            taken.add(attribute)
            is_required = wire in required
            kind = self.type_of(sub, f"{name}{pascal(wire)}")
            resolved = self.resolve(sub)
            resolved = resolved if isinstance(resolved, dict) else {}
            default = None
            raw_default = resolved.get("default")
            if not is_required and isinstance(raw_default, _SCALAR_DEFAULTS) and _is_scalar_type(kind):
                default = quote(raw_default) if isinstance(raw_default, str) else repr(raw_default)
            if not is_required and default is None:
                kind = kind.with_none()
            model.fields.append(FieldDef(attribute, wire, kind, is_required, default, resolved.get("description", "")))
        return model

    # -- results --------------------------------------------------------------------------------------

    def finish(self) -> tuple[dict[str, ModelDef], dict[str, AliasDef]]:
        return self._models, self._aliases


def _is_scalar_type(kind: TypeRef) -> bool:
    # Only a plain value of the field's own type can serve as a default; anything else is left unset.
    return kind.expr in _PRIMITIVES.values() or kind.expr.startswith(("Literal[", "Annotated["))


def _literal(values: list[Any]) -> TypeRef:
    parts = ", ".join("None" if v is None else quote(v) if isinstance(v, str) else repr(v) for v in values)
    return TypeRef(f"Literal[{parts}]", imports=frozenset({"Literal"}), nullable=None in values)
