"""Original response-schema assertions, unchanged."""
from tests.modules.mcp_response_values.fixtures import MANDATORY_FIELDS

_SCALAR_FIELD_TYPES = {
    "schema_version": "string",
    "generated_at": "string",
    "index_timestamp": ["string", "null"],
    "source_commit": ["string", "null"],
    "freshness": "string",
    "fallback": "boolean",
    "partial": "boolean",
}


def _schema_properties() -> tuple[dict, dict]:
    from llm_wiki.mcp_response_values import envelope_schema

    schema = envelope_schema()
    return schema, schema["properties"]


def test_envelope_schema_declares_every_field_type():
    schema, properties = _schema_properties()

    assert (set(schema["required"]), schema["additionalProperties"]) == (MANDATORY_FIELDS, False)
    assert {name: properties[name]["type"] for name in _SCALAR_FIELD_TYPES} == _SCALAR_FIELD_TYPES
    assert (properties["generated_at"]["format"], set(properties["freshness"]["enum"])) == (
        "date-time",
        {"fresh", "stale", "unknown"},
    )


def test_envelope_schema_declares_numeric_bounds_and_nested_shapes():
    _schema, properties = _schema_properties()
    bounded = {"type": "number", "minimum": 0, "maximum": 1}
    assert (properties["coverage"], properties["confidence"]) == (bounded, bounded)
    assert properties["warnings"] == {"type": "array", "items": {"type": "string"}}
    assert properties["components"]["additionalProperties"] == {
        "type": "object",
        "properties": {
            "generation": {"type": ["string", "null"]},
            "freshness": {
                "type": "string",
                "enum": ["fresh", "stale", "missing", "unknown"],
            },
        },
        "required": ["generation", "freshness"],
        "additionalProperties": False,
    }
    assert properties["data"] == {}
