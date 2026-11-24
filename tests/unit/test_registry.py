from __future__ import annotations

import json
import types

import pytest

from xray_cloud.errors import ToolError
from xray_cloud.registry import REGISTRY, Registry, coerce


def array_schema(name):
    return {"type": "object", "properties": {name: {"type": "array", "items": {"type": "string"}}}}


def test_json_string_arrays_are_coerced():
    assert coerce({"testKeys": '["P-1","P-2"]'}, array_schema("testKeys")) == {"testKeys": ["P-1", "P-2"]}


def test_json_string_objects_are_coerced():
    schema = {"type": "object", "properties": {"o": {"type": "object"}}}
    assert coerce({"o": '{"a": 1}'}, schema) == {"o": {"a": 1}}


def test_numeric_string_is_coerced_to_integer():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert coerce({"n": "42"}, schema) == {"n": 42}


def test_bad_coercion_is_tool_error():
    with pytest.raises(ToolError):
        coerce({"testKeys": "not json"}, array_schema("testKeys"))


def test_missing_required_param_is_tool_error():
    schema = {"type": "object", "required": ["k"], "properties": {"k": {"type": "string"}}}
    with pytest.raises(ToolError) as e:
        coerce({}, schema)
    assert "k" in e.value.args[0]


def test_unknown_param_is_tool_error_naming_key():
    schema = {"type": "object", "additionalProperties": False, "properties": {"k": {"type": "string"}}}
    with pytest.raises(ToolError) as e:
        coerce({"k": "a", "zzz": 1}, schema)
    assert "zzz" in e.value.args[0]


def test_unknown_param_rejected_even_without_additional_properties_flag():
    schema = {"type": "object", "properties": {"k": {"type": "string"}}}
    with pytest.raises(ToolError):
        coerce({"zzz": 1}, schema)


@pytest.mark.parametrize("schema,value", [
    ({"type": "string"}, 5),
    ({"type": "integer"}, 1.5),
    ({"type": "integer"}, True),
    ({"type": "boolean"}, "yes"),
    ({"type": ["string", "null"]}, 3),
    ({"type": "string", "enum": ["a", "b"]}, "c"),
    ({"type": "integer", "minimum": 1}, 0),
    ({"type": "array", "minItems": 1}, []),
    ({"type": "array", "items": {"type": "string"}}, [1]),
])
def test_invalid_values_rejected(schema, value):
    with pytest.raises(ToolError):
        coerce({"v": value}, {"type": "object", "properties": {"v": schema}})


@pytest.mark.parametrize("schema,value", [
    ({"type": ["string", "null"]}, None),
    ({"type": "number"}, 1.5),
    ({"type": "number"}, 2),
    ({"type": "string", "enum": ["a"]}, "a"),
    ({"type": "array", "minItems": 1}, [1]),
])
def test_valid_values_accepted(schema, value):
    assert coerce({"v": value}, {"type": "object", "properties": {"v": schema}}) == {"v": value}


def test_registry_call_validates_and_invokes():
    reg = Registry()
    seen = {}

    @reg.tool("t", "d", {"type": "object", "required": ["keys"], "properties": array_schema("keys")["properties"]},
              write=True)
    def t(ctx, args):
        seen["ctx"], seen["args"] = ctx, args
        return {"ok": True}

    assert reg.get("t").write is True
    ctx = types.SimpleNamespace(read_only=False)
    res = reg.call(ctx, "t", {"keys": '["A"]'})
    assert seen == {"ctx": ctx, "args": {"keys": ["A"]}}
    assert json.loads(res["content"][0]["text"]) == {"ok": True}
    assert reg.list()[0]["name"] == "t"
    assert reg.list()[0]["inputSchema"]["required"] == ["keys"]


def test_registry_call_missing_param_is_error_result():
    reg = Registry()
    reg.tool("t", "d", {"type": "object", "required": ["k"], "properties": {"k": {"type": "string"}}})(
        lambda ctx, args: {})
    res = reg.call(None, "t", {})
    assert res["isError"] is True


def test_module_level_registry_exists():
    assert isinstance(REGISTRY, Registry)


def test_maximum_enforced():
    schema = {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1,
                                                         "maximum": 100}}}
    assert coerce({"limit": "100"}, schema) == {"limit": 100}
    with pytest.raises(ToolError, match="<= 100"):
        coerce({"limit": 101}, schema)


def test_every_limit_param_is_capped_at_100():
    import xray_cloud.tools  # noqa: F401
    from xray_cloud.registry import REGISTRY
    limits = [(t["name"], t["inputSchema"]["properties"]["limit"]) for t in REGISTRY.list()
              if "limit" in t["inputSchema"].get("properties", {})]
    assert limits
    # find_related_tests caps the merged result set (paged internally), not one Xray page
    assert [n for n, s in limits
            if s.get("maximum") != (200 if n == "find_related_tests" else 100)] == []


def test_null_for_optional_parameter_is_treated_as_omitted():
    schema = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "string"}, "aiLabel": {"type": "string"},
        "assigneeId": {"type": "string"}}}
    assert coerce({"a": "x", "aiLabel": None}, schema) == coerce({"a": "x"}, schema) == {"a": "x"}
    assert coerce({"a": "x", "assigneeId": None}, schema) == {"a": "x", "assigneeId": None}
    with pytest.raises(ToolError):
        coerce({"a": None}, schema)  # required stays required
