from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ValidationIssue:
    path: str
    message: str


class SchemaValidator:
    """Small JSON-schema-like validator with strict object field checks."""

    def validate(self, instance: Any, schema: dict[str, Any], path: str = "$") -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        self._validate(instance, schema, path, issues)
        return issues

    def _validate(self, value: Any, schema: dict[str, Any], path: str, issues: list[ValidationIssue]) -> None:
        expected_type = schema.get("type")
        if expected_type and not self._type_ok(value, expected_type):
            issues.append(ValidationIssue(path, f"expected type {expected_type}, got {type(value).__name__}"))
            return

        if isinstance(expected_type, list):
            # handled by _type_ok
            pass

        if "enum" in schema and value not in schema["enum"]:
            issues.append(ValidationIssue(path, f"value {value!r} not in enum {schema['enum']}"))

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            exclusive_min = schema.get("exclusiveMinimum")
            exclusive_max = schema.get("exclusiveMaximum")
            if minimum is not None and value < minimum:
                issues.append(ValidationIssue(path, f"value {value} is below minimum {minimum}"))
            if maximum is not None and value > maximum:
                issues.append(ValidationIssue(path, f"value {value} is above maximum {maximum}"))
            if exclusive_min is not None and value <= exclusive_min:
                issues.append(ValidationIssue(path, f"value {value} must be > {exclusive_min}"))
            if exclusive_max is not None and value >= exclusive_max:
                issues.append(ValidationIssue(path, f"value {value} must be < {exclusive_max}"))

        if isinstance(value, str):
            min_len = schema.get("minLength")
            if min_len is not None and len(value) < min_len:
                issues.append(ValidationIssue(path, f"string length {len(value)} below minLength {min_len}"))

        if isinstance(value, list):
            min_items = schema.get("minItems")
            if min_items is not None and len(value) < min_items:
                issues.append(ValidationIssue(path, f"array length {len(value)} below minItems {min_items}"))
            items_schema = schema.get("items")
            if items_schema:
                for idx, item in enumerate(value):
                    self._validate(item, items_schema, f"{path}[{idx}]", issues)

        if isinstance(value, dict):
            required = schema.get("required", [])
            for field in required:
                if field not in value:
                    issues.append(ValidationIssue(path, f"missing required field '{field}'"))
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            for key in value:
                if key in properties:
                    self._validate(value[key], properties[key], f"{path}.{key}", issues)
                elif additional is False:
                    issues.append(ValidationIssue(path, f"unexpected field '{key}'"))

    @staticmethod
    def _type_ok(value: Any, expected: str | list[str]) -> bool:
        allowed = expected if isinstance(expected, list) else [expected]
        for typ in allowed:
            if typ == "object" and isinstance(value, dict):
                return True
            if typ == "array" and isinstance(value, list):
                return True
            if typ == "string" and isinstance(value, str):
                return True
            if typ == "boolean" and isinstance(value, bool):
                return True
            if typ == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
            if typ == "integer" and isinstance(value, int) and not isinstance(value, bool):
                return True
            if typ == "null" and value is None:
                return True
        return False
