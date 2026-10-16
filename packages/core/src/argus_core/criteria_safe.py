"""Safe, bounded acceptance checks for local model answers (no regex, no jsonschema)."""

from __future__ import annotations

import json
from typing import Any

MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_NODES = 48
MAX_SCHEMA_JSON_BYTES = 8192

_FORBIDDEN_SCHEMA_KEYS = frozenset(
    {
        "$ref",
        "$defs",
        "$id",
        "$schema",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
        "pattern",
        "patternProperties",
        "format",
        "contentEncoding",
        "contentMediaType",
        "if",
        "then",
        "else",
        "not",
        "allOf",
        "anyOf",
        "oneOf",
        "dependentSchemas",
        "dependentRequired",
        "unevaluatedProperties",
        "unevaluatedItems",
        "prefixItems",
    }
)

_ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "enum",
        "items",
        "additionalProperties",
    }
)


def criteria_is_effective(criteria: Any) -> bool:
    """True when at least one check can verify an answer."""
    if criteria.must_contain:
        return True
    if criteria.equals is not None:
        return True
    if criteria.max_chars is not None:
        return True
    return criteria.json_schema is not None


def assert_schema_definition(schema: object) -> None:
    """Reject unsafe or oversized JSON Schema before any model call."""
    if not isinstance(schema, dict):
        raise ValueError("json_schema must be a JSON object")
    raw = json.dumps(schema, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_SCHEMA_JSON_BYTES:
        raise ValueError(f"json_schema exceeds {MAX_SCHEMA_JSON_BYTES} bytes")
    nodes = 0

    def walk(node: object, depth: int) -> None:
        nonlocal nodes
        if depth > MAX_SCHEMA_DEPTH:
            raise ValueError(f"json_schema exceeds max depth {MAX_SCHEMA_DEPTH}")
        if not isinstance(node, dict):
            raise ValueError("json_schema nodes must be objects")
        nodes += 1
        if nodes > MAX_SCHEMA_NODES:
            raise ValueError(f"json_schema exceeds {MAX_SCHEMA_NODES} nodes")
        for key in node:
            if key in _FORBIDDEN_SCHEMA_KEYS:
                raise ValueError(f"json_schema keyword {key!r} is not allowed")
            if key not in _ALLOWED_SCHEMA_KEYS:
                raise ValueError(f"json_schema keyword {key!r} is not allowed")
        if "type" in node:
            _check_type_keyword(node)
        if "properties" in node:
            props = node["properties"]
            if not isinstance(props, dict):
                raise ValueError("properties must be an object")
            for sub in props.values():
                walk(sub, depth + 1)
        if "items" in node:
            walk(node["items"], depth + 1)
        if "enum" in node:
            enum = node["enum"]
            if not isinstance(enum, list) or not enum:
                raise ValueError("enum must be a non-empty array")
            if len(enum) > 64:
                raise ValueError("enum may have at most 64 values")

    walk(schema, 0)


def _check_type_keyword(node: dict[str, Any]) -> None:
    t = node["type"]
    if t not in ("object", "array", "string", "integer", "number", "boolean", "null"):
        raise ValueError(f"unsupported json_schema type {t!r}")
    if (
        t == "object"
        and "additionalProperties" in node
        and node["additionalProperties"] is not False
    ):
        raise ValueError("additionalProperties must be false when present")


def _validate_value(value: Any, schema: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return ["expected a JSON object"]
        required = schema.get("required", [])
        if not isinstance(required, list):
            return ["invalid required in schema"]
        failures.extend(
            f"missing property {key!r}" for key in required if key not in value
        )
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            return ["invalid properties in schema"]
        for key, subschema in props.items():
            if key in value and isinstance(subschema, dict):
                failures.extend(_validate_value(value[key], subschema))
        return failures
    if t == "array":
        if not isinstance(value, list):
            return ["expected a JSON array"]
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for item in value:
                failures.extend(_validate_value(item, item_schema))
        return failures
    if t == "string":
        if not isinstance(value, str):
            return ["expected a string"]
        if "enum" in schema and value not in schema["enum"]:
            return [f"string not in enum {schema['enum']!r}"]
        return failures
    if t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return ["expected an integer"]
        if "enum" in schema and value not in schema["enum"]:
            return [f"integer not in enum {schema['enum']!r}"]
        return failures
    if t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return ["expected a number"]
        return failures
    if t == "boolean":
        if not isinstance(value, bool):
            return ["expected a boolean"]
        return failures
    if t == "null":
        if value is not None:
            return ["expected null"]
        return failures
    return [f"unsupported schema type {t!r}"]


def check_answer(text: str | None, criteria: Any) -> list[str]:
    """Deterministic acceptance; empty list only when every check passes."""
    if not text or not text.strip():
        return ["empty answer"]
    failures: list[str] = []
    if criteria.max_chars is not None and len(text) > criteria.max_chars:
        failures.append(f"longer than {criteria.max_chars} characters")
    failures += [f"missing {s!r}" for s in criteria.must_contain if s not in text]
    if criteria.equals is not None and text.strip() != criteria.equals:
        failures.append(f"does not equal {criteria.equals!r}")
    if criteria.json_schema is not None:
        try:
            parsed = json.loads(text)
        except ValueError:
            failures.append("not valid JSON")
        else:
            failures.extend(_validate_value(parsed, criteria.json_schema))
    return failures
