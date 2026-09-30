"""
to_strict_schema() feeds output_config.format, which rejects several JSON
Schema keywords Pydantic emits freely. If this drifts, every AI call 400s.
"""

from typing import Optional

from pydantic import BaseModel, Field

from app.core.claude_client import to_strict_schema


class Inner(BaseModel):
    label: str
    weight: float = Field(ge=0, le=1)


class Outer(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    count: int = Field(ge=0)
    tags: list[str] = Field(default=[], max_length=10)
    note: Optional[str] = None
    inner: Optional[Inner] = None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_unsupported_keywords_are_stripped():
    schema = to_strict_schema(Outer)
    banned = {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minLength", "maxLength", "pattern",
        "minItems", "maxItems", "uniqueItems", "default", "examples",
    }
    for node in _walk(schema):
        assert not (banned & set(node)), f"leaked constraint in {node}"


def test_every_object_forbids_additional_properties():
    schema = to_strict_schema(Outer)
    objects = [n for n in _walk(schema) if "properties" in n]
    assert objects, "expected at least the root object and Inner"
    for node in objects:
        assert node.get("additionalProperties") is False


def test_every_property_is_required():
    """Optionality is expressed by allowing null, not by omitting from
    `required` — the strict validator needs the full list."""
    schema = to_strict_schema(Outer)
    for node in _walk(schema):
        if "properties" in node:
            assert set(node["required"]) == set(node["properties"].keys())


def test_nested_model_definitions_survive():
    schema = to_strict_schema(Outer)
    assert "$defs" in schema
    assert "Inner" in schema["$defs"]
    assert schema["$defs"]["Inner"]["additionalProperties"] is False


def test_optional_field_still_allows_null():
    schema = to_strict_schema(Outer)
    note = schema["properties"]["note"]
    rendered = str(note)
    assert "null" in rendered, f"Optional[str] lost its null branch: {note}"
