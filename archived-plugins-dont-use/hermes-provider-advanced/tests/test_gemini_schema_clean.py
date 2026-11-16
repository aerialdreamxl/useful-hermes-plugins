"""Golden tests for advlib.gemini_schema_clean, ported from CLIProxyAPI's
internal/util/gemini_schema_test.go and gemini_schema_ref_test.go expectations.

Run directly (stdlib only, no pytest needed):
    python tests/test_gemini_schema_clean.py
Exits 0 when everything passes.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SPEC = importlib.util.spec_from_file_location(
    "gemini_schema_clean",
    os.path.join(_ROOT, "advlib", "gemini_schema_clean.py"))
gsc = importlib.util.module_from_spec(_SPEC)
sys.modules["gemini_schema_clean"] = gsc
_SPEC.loader.exec_module(gsc)


# --- helpers ---------------------------------------------------------------------

def J(text):
    return json.loads(text)


def out(node):
    """Recursively rewrite for stable comparisons (json equality is via ==)."""
    return node


def validated_tool(schema):
    """CPA CleanJSONSchemaForAntigravity = Tool(schema, requirePlaceholder=true)."""
    return gsc.antigravity_tool_schema(schema, require_placeholder=True,
                                       flatten_multi_branch_unions=False)


def legacy_tool(schema):
    """CPA CleanJSONSchemaForAntigravityTool(schema, false)."""
    return gsc.antigravity_tool_schema(schema, require_placeholder=False,
                                       flatten_multi_branch_unions=False)


def claude_tool(schema, require_placeholder=False):
    """CPA CleanJSONSchemaForAntigravityClaudeTool(schema, requirePlaceholder)."""
    return gsc.antigravity_tool_schema(schema, require_placeholder=require_placeholder,
                                       flatten_multi_branch_unions=True)


def get(node, *keys):
    cur = node
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and k.isdigit() and int(k) < len(cur):
            cur = cur[int(k)]
        else:
            return None
    return cur


NO_TYPE_LIST_PRESETS = [legacy_tool, validated_tool, claude_tool,
                        gsc.clean_antigravity_tool, gsc.clean_antigravity_claude_tool,
                        gsc.clean_antigravity_response, gsc.clean_gemini]


def assert_no_type_list(node, where):
    """Upstream fingerprint 'Proto field is not repeating, cannot start list' means
    a type array survived cleaning. Antigravity/Gemini-native presets must never
    emit type as a list."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            t = cur.get("type")
            assert not isinstance(t, list), f"type is a list in {where}: {t!r}"
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


CLEANERS = {
    "antigravity": validated_tool,
    "antigravityTool": legacy_tool,
    "antigravityClaude": claude_tool,
    "antigravityResponse": gsc.clean_antigravity_response,
    "gemini": gsc.clean_gemini,
    "geminiJSONSchema": gsc.clean_gemini_json_schema,
}
NO_PLACEHOLDER_CLEANERS = {k: v for k, v in CLEANERS.items() if k != "antigravity"}


# --- gemini_schema_test.go goldens ------------------------------------------------

def test_const_to_enum():
    # TestCleanJSONSchemaForAntigravity_ConstToEnum
    inp = {"type": "object", "properties": {"kind": {"type": "string", "const": "InsightVizNode"}}}
    exp = {"type": "object", "properties": {"kind": {"type": "string", "description": "Allowed: InsightVizNode"}}}
    assert validated_tool(inp) == exp


def test_type_flattening_nullable():
    # TestCleanJSONSchemaForAntigravity_TypeFlattening_Nullable
    inp = {"type": "object",
           "properties": {"name": {"type": ["string", "null"]}, "other": {"type": "string"}},
           "required": ["name", "other"]}
    exp = {"type": "object",
           "properties": {"name": {"type": "string", "nullable": True, "description": "(nullable)"},
                          "other": {"type": "string"}},
           "required": ["name", "other"]}
    got = validated_tool(inp)
    assert got == exp, json.dumps(got)


def test_constraints_to_description():
    # TestCleanJSONSchemaForAntigravity_ConstraintsToDescription
    inp = {"type": "object", "properties": {
        "tags": {"type": "array", "description": "List of tags", "minItems": 1},
        "name": {"type": "string", "description": "User name", "minLength": 3}}}
    r = json.dumps(validated_tool(inp))
    assert '"minItems"' not in r and "minItems: 1" in r
    assert '"minLength"' not in r and "minLength: 3" in r


def test_anyof_flattening_smart_selection():
    # TestCleanJSONSchemaForAntigravity_AnyOfFlattening_SmartSelection
    inp = {"type": "object", "properties": {"query": {"anyOf": [
        {"type": "null"},
        {"type": "object", "properties": {"kind": {"type": "string"}}}]}}}
    exp = {"type": "object", "properties": {"query": {
        "type": "object", "nullable": True, "description": "Accepts: null | object",
        "properties": {"_": {"type": "boolean"}, "kind": {"type": "string"}},
        "required": ["_"]}}}
    assert validated_tool(inp) == exp


def test_oneof_flattening():
    # TestCleanJSONSchemaForAntigravity_OneOfFlattening (native carrier: oneOf -> anyOf preserved)
    inp = {"type": "object", "properties": {"config": {"oneOf": [
        {"type": "string"}, {"type": "integer"}]}}}
    exp = {"type": "object", "properties": {"config": {"anyOf": [
        {"type": "string"}, {"type": "integer"}]}}}
    assert validated_tool(inp) == exp


def test_allof_merging():
    # TestCleanJSONSchemaForAntigravity_AllOfMerging (required union)
    inp = {"type": "object", "allOf": [
        {"properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"properties": {"b": {"type": "integer"}}, "required": ["b"]}]}
    exp = {"type": "object",
           "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
           "required": ["a", "b"]}
    assert validated_tool(inp) == exp


def test_ref_handling():
    # TestCleanJSONSchemaForAntigravity_RefHandling: local $ref inlined before definitions die
    inp = {"definitions": {"User": {"type": "object", "properties": {"name": {"type": "string"}}}},
           "type": "object", "properties": {"customer": {"$ref": "#/definitions/User"}}}
    exp = {"type": "object", "properties": {"customer": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "_": {"type": "boolean"}},
        "required": ["_"]}}}
    assert validated_tool(inp) == exp


def test_ref_handling_description_escaping():
    # TestCleanJSONSchemaForAntigravity_RefHandling_DescriptionEscaping
    inp = {"definitions": {"User": {"type": "object", "properties": {"name": {"type": "string"}}}},
           "type": "object",
           "properties": {"customer": {"description": "He said \"hi\"\nsecond line",
                                       "$ref": "#/definitions/User"}}}
    got = validated_tool(inp)
    assert get(got, "properties", "customer", "description") == "He said \"hi\"\nsecond line"
    assert get(got, "properties", "customer", "properties", "name", "type") == "string"
    assert get(got, "properties", "customer", "properties", "_") == {"type": "boolean"}


def test_cyclic_ref_defaults():
    # TestCleanJSONSchemaForAntigravity_CyclicRefDefaults
    inp = {"definitions": {"Node": {"type": "object",
                                    "properties": {"child": {"$ref": "#/definitions/Node"}}}},
           "$ref": "#/definitions/Node"}
    got = validated_tool(inp)
    assert got["type"] == "object"
    child = get(got, "properties", "child")
    assert child["type"] == "object" and "Node" in child["description"], json.dumps(got)


def test_required_cleanup():
    # TestCleanJSONSchemaForAntigravity_RequiredCleanup
    inp = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
           "required": ["a", "b", "c"]}
    exp = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
           "required": ["a", "b"]}
    assert validated_tool(inp) == exp


def test_allof_merging_dot_keys():
    # TestCleanJSONSchemaForAntigravity_AllOfMerging_DotKeys
    inp = {"type": "object", "allOf": [
        {"properties": {"my.param": {"type": "string"}}, "required": ["my.param"]},
        {"properties": {"b": {"type": "integer"}}, "required": ["b"]}]}
    exp = {"type": "object",
           "properties": {"my.param": {"type": "string"}, "b": {"type": "integer"}},
           "required": ["my.param", "b"]}
    assert validated_tool(inp) == exp


def test_property_name_collision():
    # TestCleanJSONSchemaForAntigravity_PropertyNameCollision
    inp = {"type": "object", "properties": {"pattern": {"type": "string", "description": "The regex pattern"}},
           "required": ["pattern"]}
    got = validated_tool(inp)
    assert got == inp
    assert "description" not in get(got, "properties")


def test_dot_keys_ref():
    # TestCleanJSONSchemaForAntigravity_DotKeys
    inp = {"type": "object", "properties": {"my.param": {"type": "string", "$ref": "#/definitions/MyType"}},
           "definitions": {"MyType": {"type": "string"}}}
    got = validated_tool(inp)
    props = got["properties"]
    assert "my.param" in props and "my" not in props
    assert "$ref" not in props["my.param"]
    assert props["my.param"]["type"] == "string"


def test_anyof_alternative_hints():
    # TestCleanJSONSchemaForAntigravity_AnyOfAlternativeHints
    inp = {"type": "object", "properties": {"value": {"anyOf": [
        {"type": "string"}, {"type": "integer"}, {"type": "null"}]}}}
    exp = {"type": "object", "properties": {"value": {
        "anyOf": [{"type": "string"}, {"type": "integer"}],
        "nullable": True, "description": "(nullable)"}}}
    assert validated_tool(inp) == exp


def test_nullable_hint():
    # TestCleanJSONSchemaForAntigravity_NullableHint
    inp = {"type": "object", "properties": {"name": {"type": ["string", "null"], "description": "User name"}},
           "required": ["name"]}
    r = json.dumps(validated_tool(inp))
    assert "(nullable)" in r and "User name" in r


def test_type_flattening_nullable_dot_key():
    # TestCleanJSONSchemaForAntigravity_TypeFlattening_Nullable_DotKey
    inp = {"type": "object", "properties": {"my.param": {"type": ["string", "null"]},
                                            "other": {"type": "string"}},
           "required": ["my.param", "other"]}
    exp = {"type": "object", "properties": {
        "my.param": {"type": "string", "nullable": True, "description": "(nullable)"},
        "other": {"type": "string"}},
        "required": ["my.param", "other"]}
    assert validated_tool(inp) == exp


def test_enum_hint():
    # TestCleanJSONSchemaForAntigravity_EnumHint
    inp = {"type": "object", "properties": {"status": {
        "type": "string", "enum": ["active", "inactive", "pending"], "description": "Current status"}}}
    r = json.dumps(validated_tool(inp))
    assert "Allowed:" in r and "active" in r and "inactive" in r


def test_additional_properties_hint():
    # TestCleanJSONSchemaForAntigravity_AdditionalPropertiesHint
    inp = {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False}
    assert "No extra properties allowed" in json.dumps(validated_tool(inp))


def test_anyof_flattening_preserves_description():
    # TestCleanJSONSchemaForAntigravity_AnyOfFlattening_PreservesDescription
    nullable_inp = {"type": "object", "properties": {"config": {
        "description": "Parent desc",
        "anyOf": [{"type": "string", "description": "Child desc"}, {"type": "null"}]}}}
    exp_nullable = {"type": "object", "properties": {"config": {
        "type": "string", "nullable": True,
        "description": "Parent desc (Child desc) (Accepts: string | null)"}}}
    assert validated_tool(nullable_inp) == exp_nullable

    multi_inp = {"type": "object", "properties": {"config": {
        "description": "Parent desc",
        "anyOf": [{"type": "string", "description": "Child desc"}, {"type": "integer"}]}}}
    assert validated_tool(multi_inp) == multi_inp


def test_single_enum_becomes_hint():
    # TestCleanJSONSchemaForAntigravity_SingleEnumBecomesHint
    inp = {"type": "object", "properties": {"kind": {"type": "string", "enum": ["fixed"]}}}
    got = validated_tool(inp)
    assert "Allowed: fixed" in get(got, "properties", "kind", "description")
    assert "enum" not in get(got, "properties", "kind")


def test_multiple_non_null_types():
    # TestCleanJSONSchemaForAntigravity_MultipleNonNullTypes
    inp = {"type": "object", "properties": {"value": {"type": ["string", "integer", "boolean"]}}}
    r = json.dumps(validated_tool(inp))
    assert "Accepts:" in r and "string" in r and "integer" in r and "boolean" in r


def test_empty_schema_placeholder():
    # TestCleanJSONSchemaForAntigravity_EmptySchemaPlaceholder (VALIDATED 'reason' rule)
    got = validated_tool({"type": "object"})
    assert got == {"type": "object",
                   "properties": {"reason": {"type": "string",
                                             "description": "Brief explanation of why you are calling this tool"}},
                   "required": ["reason"]}
    got2 = gsc.clean_antigravity_claude_tool({"type": "object"})
    assert got2 == got


def test_empty_properties_placeholder():
    # TestCleanJSONSchemaForAntigravity_EmptyPropertiesPlaceholder
    assert "reason" in json.dumps(validated_tool({"type": "object", "properties": {}}))


def test_non_empty_schema_unchanged():
    # TestCleanJSONSchemaForAntigravity_NonEmptySchemaUnchanged
    inp = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    got = validated_tool(inp)
    assert "reason" not in json.dumps(got) and "name" in json.dumps(got)


def test_nested_empty_schema():
    # TestCleanJSONSchemaForAntigravity_NestedEmptySchema
    inp = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}}
    got = validated_tool(inp)
    assert get(got, "properties", "items", "items", "properties", "reason") is not None


def test_empty_schema_with_description():
    # TestCleanJSONSchemaForAntigravity_EmptySchemaWithDescription
    got = validated_tool({"type": "object", "description": "An empty object"})
    assert "An empty object" in got["description"] and "reason" in json.dumps(got)


def test_response_no_tool_placeholders():
    # TestCleanJSONSchemaForAntigravityResponseDoesNotAddToolPlaceholders
    inp = {"type": "object", "title": "Response", "nullable": True,
           "properties": {"empty": {"type": "object"},
                          "optional": {"type": "object", "properties": {"value": {"type": "string"}}}}}
    got = gsc.clean_antigravity_response(inp)
    for forbidden in [("properties", "empty", "properties", "reason"), ("properties", "empty", "required"),
                      ("properties", "optional", "properties", "_"), ("properties", "optional", "required")]:
        assert get(got, *forbidden) is None, json.dumps(got)
    assert got.get("title") == "Response" and got.get("nullable") is True


def test_response_projects_ignored_unions():
    # TestCleanJSONSchemaForAntigravityResponseProjectsIgnoredUnions
    inp = {"type": "object", "properties": {
        "action": {"anyOf": [{"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                             {"type": "null"}]},
        "label": {"oneOf": [{"type": "string"}, {"type": "null"}]}}}
    got = gsc.clean_antigravity_response(inp)
    assert "anyOf" not in get(got, "properties", "action")
    assert "oneOf" not in get(got, "properties", "label")
    assert get(got, "properties", "action", "type") == "object"
    assert get(got, "properties", "action", "nullable") is True
    assert get(got, "properties", "label", "type") == "string"
    assert get(got, "properties", "label", "nullable") is True


def test_response_preserves_additional_properties_false():
    # TestCleanJSONSchemaForAntigravityResponsePreservesAdditionalPropertiesFalse
    inp = {"type": "object",
           "properties": {"name": {"type": "string"},
                          "nested": {"type": "object",
                                     "properties": {"age": {"type": "integer"}},
                                     "additionalProperties": False}},
           "additionalProperties": False}
    got = gsc.clean_antigravity_response(inp)
    assert got["additionalProperties"] is False
    assert get(got, "properties", "nested", "additionalProperties") is False
    assert "No extra properties allowed" not in json.dumps(got)

    tool = validated_tool(inp)
    assert "additionalProperties" not in json.dumps(tool)
    assert "No extra properties allowed" in json.dumps(tool)

    non_false = {"type": "object",
                 "properties": {"map": {"type": "object", "additionalProperties": {"type": "string"}}},
                 "additionalProperties": True}
    assert "additionalProperties" not in json.dumps(gsc.clean_antigravity_response(non_false))


def test_response_preserves_enum_type():
    # TestCleanJSONSchemaForAntigravityResponsePreservesEnumType
    inp = {"type": "object", "properties": {
        "conviction": {"type": "number", "enum": [0.25, 0.5, 1]},
        "count": {"type": "integer", "enum": [1, 2]}}}
    got = gsc.clean_antigravity_response(inp)
    assert get(got, "properties", "conviction", "type") == "number"
    assert get(got, "properties", "conviction", "enum") == ["0.25", "0.5", "1"]
    assert get(got, "properties", "count", "type") == "integer"
    assert get(got, "properties", "count", "enum") == ["1", "2"]


def test_format_field_removal():
    # TestCleanJSONSchemaForAntigravity_FormatFieldRemoval / NoDescription / MultipleFormats
    inp = {"type": "object", "properties": {"url": {"type": "string", "format": "uri", "description": "A URL"}}}
    r = json.dumps(validated_tool(inp))
    assert '"format"' not in r and "format: uri" in r and "A URL" in r

    inp2 = {"type": "object", "properties": {"email": {"type": "string", "format": "email"}}}
    r2 = json.dumps(validated_tool(inp2))
    assert '"format"' not in r2 and "format: email" in r2

    inp3 = {"type": "object", "properties": {
        "url": {"type": "string", "format": "uri"},
        "email": {"type": "string", "format": "email"},
        "date": {"type": "string", "format": "date-time"}}}
    r3 = json.dumps(validated_tool(inp3))
    assert '"format"' not in r3 and "format: uri" in r3 and "format: email" in r3 and "format: date-time" in r3


def test_tool_enums_become_hints():
    # TestCleanJSONSchemaForAntigravity_ToolEnumsBecomeHints (types are NOT rewritten)
    inp = {"type": "object", "properties": {
        "priority": {"type": "integer", "enum": [0, 1, 2]},
        "level": {"type": "number", "enum": [1.5, 2.5, 3.5]},
        "status": {"type": "string", "enum": ["active", "inactive"]}}}
    got = validated_tool(inp)
    for path, want in [("properties.priority", "integer"), ("properties.level", "number"),
                       ("properties.status", "string")]:
        node = get(got, *path.split("."))
        assert node["type"] == want, json.dumps(got)
        assert "enum" not in node and "Allowed:" in node["description"], json.dumps(got)


def test_boolean_tool_enum_becomes_hint():
    # TestCleanJSONSchemaForAntigravity_BooleanToolEnumBecomesHint
    inp = {"type": "object", "properties": {"enabled": {"type": "boolean", "enum": [True, False]}}}
    got = get(validated_tool(inp), "properties", "enabled")
    assert "enum" not in got and got["type"] == "boolean"
    assert "Allowed: true, false" in got["description"]


def test_gemini_removes_unsupported_metadata():
    # TestCleanJSONSchemaForGemini_RemovesGeminiUnsupportedMetadataFields
    inp = J(r'''{"$schema": "http://json-schema.org/draft-07/schema#","$id": "root-schema",
    "$comment": "root comment should be removed","type": "object",
    "properties": {"payload": {"type": "object","$comment": "nested","prefill": "hello",
        "properties": {"mode": {"type": "string","enum": ["a", "b"],
                                "enumDescriptions": ["Alpha", "Beta"],"enumTitles": ["A", "B"]}},
        "patternProperties": {"^x-": {"type": "string"}}},
      "$id": {"type": "string","description": "property name should not be removed"},
      "$comment": {"type": "string","description": "property name should not be removed"},
      "enumDescriptions": {"type": "array","description": "property name should not be removed"}}}''')
    exp = J(r'''{"type": "object","properties": {"payload": {"type": "object","properties": {
            "mode": {"type": "string","enum": ["a", "b"],"description": "Allowed: a, b"}}},
        "$id": {"type": "string","description": "property name should not be removed"},
        "$comment": {"type": "string","description": "property name should not be removed"},
        "enumDescriptions": {"type": "array","items": {"type": "string"},
                             "description": "property name should not be removed"}}}''')
    assert gsc.clean_gemini(inp) == exp


def test_extension_fields_removed():
    # TestRemoveExtensionFields (via the pipeline; x-* names under properties are author names)
    inp = J(r'''{"type": "object","x-custom-meta": "value","x-root.meta": "remove",
        "properties": {"foo": {"type": "string","x-internal-id": 123},
                       "x-data": {"type": "string"},
                       "normal": {"type": "number","x-meta": "remove"},
                       "foo.bar": {"type": "string","x-meta": "remove"}},"required": ["x-data"]}''')
    got = gsc.clean_gemini(inp)
    assert "x-" not in json.dumps(got) or all(
        k.startswith("x-") and k in get(got, "properties") for k in ["x-data"])
    assert get(got, "properties", "x-data") is not None
    assert "x-meta" not in json.dumps(got) and "x-root.meta" not in json.dumps(got)


def test_unique_items_stripped():
    # TestCleanJSONSchemaForAntigravity_UniqueItemsStripped
    inp = {"type": "object", "properties": {"ids": {
        "type": "array", "description": "Unique identifiers",
        "items": {"type": "string"}, "uniqueItems": True}}}
    r = json.dumps(validated_tool(inp))
    assert '"uniqueItems"' not in r and "uniqueItems: true" in r


def test_is_property_definition_table():
    # TestIsPropertyDefinitionDistinguishesPropertyNamedProperties
    table = {
        "": False, "properties": True, "properties.properties": False,
        "properties.properties.properties": True,
        "properties.records.items.properties": True, "properties.records.items": False,
        "schema.properties": True,
        "request.tools.0.functionDeclarations.0.parameters": False,
        "request.tools.0.functionDeclarations.0.parameters.properties": True,
        "request.tools.0.functionDeclarations.0.parameters.properties.properties": False,
        "$defs": True, "$defs.properties": False, "properties.$defs": False,
        "properties.a.patternProperties": True, "properties.patternProperties": False,
    }
    for path, want in table.items():
        assert gsc.is_property_definition(path) == want, path


def test_strips_property_names_under_property_named_properties():
    # TestCleanJSONSchemaStripsPropertyNamesUnderPropertyNamedProperties
    shapes = {
        "arrayItem": {"type": "object", "properties": {"records": {"type": "array", "items": {
            "type": "object", "properties": {"name": {"type": "string"}},
            "propertyNames": {"type": "string"}}}}},
        "propertyNamedProperties": {"type": "object", "properties": {"properties": {
            "type": "object", "propertyNames": {"type": "string"}}}},
        "combined": {"type": "object", "properties": {"pages": {"type": "array", "items": {
            "type": "object",
            "properties": {"properties": {"type": "object", "propertyNames": {"type": "string"},
                                           "additionalProperties": True}},
            "propertyNames": {"type": "string"}}}}},
    }
    for name, schema in shapes.items():
        for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
            got = json.dumps(CLEANERS[cleaner_name](schema))
            assert '"propertyNames"' not in got, (name, cleaner_name, got)
            assert '"additionalProperties"' not in got, (name, cleaner_name, got)


def test_keeps_properties_named_like_keywords():
    # TestCleanJSONSchemaKeepsPropertiesNamedLikeKeywords
    inp = J(r'''{"type":"object","properties":{
        "propertyNames":{"type":"string"},"patternProperties":{"type":"string"},
        "properties":{"type":"object","properties":{"propertyNames":{"type":"string"}}}}}''')
    for cleaner_name in ("antigravity", "gemini"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "propertyNames") is not None, cleaner_name
        assert get(got, "properties", "patternProperties") is not None, cleaner_name
        assert get(got, "properties", "properties", "properties", "propertyNames") is not None, cleaner_name


def test_conditional_keywords():
    # TestCleanJSONSchema_ConditionalKeywords (root / allOf / nested)
    root_inp = J(r'''{"type": "object","properties": { "kind": { "type": "string", "enum": ["buy", "sell"] }},
        "required": ["kind"],"if": { "properties": { "kind": { "const": "sell" }}},
        "then": { "properties": { "sell_reason": { "type": "string", "description": "why the position is being sold" }}, "required": ["sell_reason"]},
        "else": { "properties": { "buy_reason": { "type": "string" }}}}''')
    allof_inp = J(r'''{"type": "object","properties": { "kind": { "type": "string", "enum": ["buy", "sell"] }},
        "required": ["kind"],"allOf": [{ "if": { "properties": { "kind": { "const": "sell" }}},
        "then": { "properties": { "sell_reason": { "type": "string", "description": "why the position is being sold" }},
        "required": ["sell_reason"]}}]}''')
    nested_inp = J(r'''{"type": "object","properties": {"trade": {"type": "object",
        "properties": { "kind": { "type": "string" }},"if": { "properties": { "kind": { "const": "sell" }}},
        "then": { "properties": { "sell_reason": { "type": "string" }}}}}}''')
    for cleaner_name in ("antigravityResponse", "antigravity", "gemini"):
        clean = CLEANERS[cleaner_name]
        res = clean(root_inp)
        assert get(res, "if") is None and get(res, "then") is None and get(res, "else") is None
        assert get(res, "properties", "sell_reason") is not None
        assert get(res, "properties", "buy_reason") is not None
        assert get(res, "properties", "sell_reason", "description") == "why the position is being sold"

        res = clean(allof_inp)
        assert get(res, "allOf") is None and get(res, "if") is None
        assert '"if":' not in json.dumps(res)
        assert get(res, "properties", "sell_reason") is not None
        assert get(res, "properties", "sell_reason", "description") == "why the position is being sold"

        res = clean(nested_inp)
        assert get(res, "properties", "trade", "if") is None
        assert get(res, "properties", "trade", "then") is None
        assert get(res, "properties", "trade", "properties", "sell_reason") is not None


def test_response_conditional_cannot_overwrite_parent():
    # TestCleanJSONSchemaForAntigravityResponseConditionalCannotOverwriteParent
    inp = J(r'''{"type":"object","properties":{"kind":{"type":"string"},
        "action":{"type":"object","properties":{"full":{"type":"string"}},"required":["full"]}},
        "required":["kind","action"],
        "allOf":[{"if":{"properties":{"kind":{"const":"skip"}}},
                  "then":{"properties":{"action":{"type":"null"},"branch_only":{"type":"integer"}}}}]}''')
    got = gsc.clean_antigravity_response(inp)
    action = get(got, "properties", "action")
    assert action["type"] == "object" and get(action, "properties", "full") is not None
    assert action["required"][0] == "full"
    assert get(got, "properties", "branch_only") is not None
    assert "allOf" not in got and '"if"' not in json.dumps(got) and '"then"' not in json.dumps(got)


def test_response_inlines_local_ref():
    # TestCleanJSONSchemaForAntigravityResponseInlinesLocalRef
    inp = J(r'''{"$defs":{"Payload":{"type":"object","properties":{"id":{"type":"integer"}},"required":["id"]}},
        "type":"object","properties":{"payload":{"$ref":"#/$defs/Payload"}},"required":["payload"]}''')
    got = gsc.clean_antigravity_response(inp)
    assert "$defs" not in got and "$ref" not in json.dumps(got)
    payload = get(got, "properties", "payload")
    assert payload["type"] == "object"
    assert get(payload, "properties", "id", "type") == "integer"
    assert payload["required"][0] == "id"


def test_response_type_array_uses_native_nullable():
    # TestCleanJSONSchemaForAntigravityResponseTypeArrayUsesNativeNullable
    inp = J(r'''{"type":"object","properties":{"value":{"type":["number","null"]}},"required":["value"]}''')
    got = gsc.clean_antigravity_response(inp)
    assert get(got, "properties", "value", "type") == "number"
    assert get(got, "properties", "value", "nullable") is True
    assert got["required"][0] == "value"


def test_tool_keeps_numeric_enum_type():
    # TestCleanJSONSchemaForAntigravityToolKeepsNumericEnumType
    inp = J(r'''{"type":"object","properties":{"value":{"type":"number","enum":[1,2]}},"required":["value"]}''')
    got = legacy_tool(inp)
    value = get(got, "properties", "value")
    assert value["type"] == "number" and "enum" not in value
    assert "Allowed: 1, 2" in value["description"]


def test_response_drops_ignored_boolean_enum():
    # TestCleanJSONSchemaForAntigravityResponseDropsIgnoredBooleanEnum
    inp = J(r'''{"type":"object","properties":{"value":{"type":"boolean","enum":["true"]}},"required":["value"]}''')
    value = get(gsc.clean_antigravity_response(inp), "properties", "value")
    assert "enum" not in value and value["type"] == "boolean"
    assert "Allowed: true" in value["description"]


def test_response_hints_ignored_constraints():
    # TestCleanJSONSchemaForAntigravityResponseHintsIgnoredConstraints
    inp = J(r'''{"type":"object","properties":{"value":{"type":"number","minimum":1,"maximum":2,"not":{"enum":[1.5]}}}}''')
    value = get(gsc.clean_antigravity_response(inp), "properties", "value")
    for kw in ("minimum", "maximum", "not"):
        assert kw not in value, kw
        assert kw + ":" in value["description"], json.dumps(value)


def test_sort_by_depth_stable():
    # TestSortByDepthUsesSegmentsAndIsStable
    paths = ["root.verylong", "root.x.y", "first.same", "later.same"]
    # exercise the module-internal helper the same way Go's unit test does
    gsc._sort_by_depth(paths)
    assert paths == ["root.x.y", "root.verylong", "first.same", "later.same"]


def test_strips_encrypted_metadata():
    # TestCleanJSONSchemaStripsEncryptedMetadata
    inp = J(r'''{"type": "object","properties": {"api_key": {"type": "string","description": "API credential","encrypted": true},
        "timeout": {"type": "integer","encrypted": false},
        "nested": {"type": "object","properties": {"secret": {"type": "string","encrypted": true}}}},
        "required": ["api_key"]}''')
    for cleaner_name in ("antigravity", "gemini", "antigravityTool", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert '"encrypted"' not in json.dumps(got), cleaner_name
        assert get(got, "properties", "api_key", "type") is not None
        assert get(got, "properties", "api_key", "description") == "API credential"
        assert get(got, "properties", "nested", "properties", "secret", "type") is not None


def test_keeps_property_named_encrypted():
    # TestCleanJSONSchemaKeepsPropertyNamedEncrypted
    inp = J(r'''{"type": "object","properties": {"encrypted": {"type": "boolean",
        "description": "Whether the payload is encrypted","encrypted": true},"data": {"type": "string"}},
        "required": ["encrypted"]}''')
    for cleaner_name in ("antigravity", "gemini"):
        got = CLEANERS[cleaner_name](inp)
        node = get(got, "properties", "encrypted")
        assert node is not None and node["type"] == "boolean"
        assert "encrypted" not in node, cleaner_name


def _check_bare_map_expectations(clean, inp):
    got = clean(inp)
    assert get(got, "properties", "data", "type") == "object"
    assert get(got, "properties", "data", "properties", "parent", "type") == "string"
    assert get(got, "properties", "data", "properties", "insert_after", "type") == "string"
    assert get(got, "properties", "data", "properties", "insert_before", "type") == "string"
    assert "parent" in get(got, "properties", "data", "required")
    assert "required" not in get(got, "properties", "data", "properties", "parent")
    assert get(got, "properties", "opts", "type") == "object"
    assert get(got, "properties", "opts", "properties", "opt_fields", "type") == "string"


def test_bare_property_map_normalized():
    # TestCleanJSONSchema_BarePropertyMapNormalized (issue 5178, Asana MCP)
    inp = J(r'''{"type": "object","properties": {"data": {"parent": { "type": "string", "required": true},
        "insert_after": { "type": "string" },"insert_before": { "type": "string" }},
        "opts": {"opt_fields": { "type": "string" }}}}}'''.replace("}}}", "}}"))
    for cleaner_name in ("antigravity", "antigravityTool", "antigravityResponse", "gemini"):
        _check_bare_map_expectations(CLEANERS[cleaner_name], inp)


def test_nested_bare_property_map():
    # TestCleanJSONSchema_NestedBarePropertyMap
    inp = J(r'''{"type": "object","properties": {"data": {"workspace": { "type": "string", "required": true},
        "task": {"name": { "type": "string", "required": true },"notes": { "type": "string" }}}}}''')
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "data", "type") == "object"
        assert get(got, "properties", "data", "properties", "workspace", "type") == "string"
        assert get(got, "properties", "data", "properties", "task", "type") == "object"
        assert get(got, "properties", "data", "properties", "task", "properties", "name", "type") == "string"
        assert "workspace" in get(got, "properties", "data", "required")
        assert "name" in get(got, "properties", "data", "properties", "task", "required")


def test_bare_property_map_with_keyword_names():
    # TestCleanJSONSchema_BarePropertyMapWithKeywordNames
    inp = J(r'''{"type": "object","properties": {"data": {
        "title": { "type": "string", "required": true },"description": { "type": "string" },
        "format": { "type": "string" },"type": { "type": "string" }}}}}'''.replace("}}}", "}}"))
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "data", "type") in ("object", "string")
        assert get(got, "properties", "data", "type") == "object"
        for p in ("title", "description", "type"):
            assert get(got, "properties", "data", "properties", p, "type") == "string", p
        assert "title" in get(got, "properties", "data", "required")


def test_array_items_bare_property_map():
    # TestCleanJSONSchema_ArrayItemsBarePropertyMap
    inp = J(r'''{"type": "object","properties": {"tasks": {"type": "array","items": {
        "id": { "type": "string", "required": true },"label": { "type": "string" }}}}}''')
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "tasks", "items", "type") == "object"
        assert get(got, "properties", "tasks", "items", "properties", "id", "type") == "string"
        assert "id" in get(got, "properties", "tasks", "items", "required")


def test_tool_arrays_missing_items():
    # TestCleanJSONSchema_ToolArraysMissingItems (issue 5292)
    inp = J(r'''{"type": "object","properties": {"params": { "type": "array" },
        "values": { "type": ["array", "null"], "description": "no items" },
        "existing": { "type": "array", "items": { "type": "number" }}}}''')
    for cleaner_name in ("antigravity", "antigravityLegacy", "gemini"):
        clean = {"antigravity": validated_tool, "antigravityLegacy": legacy_tool,
                 "gemini": gsc.clean_gemini}[cleaner_name]
        got = clean(inp)
        assert get(got, "properties", "params", "items", "type") == "string"
        assert get(got, "properties", "values", "items", "type") == "string"
        assert get(got, "properties", "existing", "items", "type") == "number"
        root = clean({"type": "array"})
        assert get(root, "items", "type") == "string"


def test_response_array_missing_items_unchanged():
    # TestCleanJSONSchema_ResponseArrayMissingItemsUnchanged
    got = gsc.clean_antigravity_response(J(r'''{"type":"object","properties":{"values":{"type":"array"}}}'''))
    assert get(got, "properties", "values", "items") is None


def test_boolean_required_promoted():
    # TestCleanJSONSchema_BooleanRequiredPromoted
    inp = J(r'''{"type": "object","properties": {"existing": { "type": "string" },
        "name": { "type": "string", "required": true },"age": { "type": "integer", "required": false },
        "tag": { "type": "string" }},"required": ["existing"]}''')
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        req = got["required"]
        assert "existing" in req and "name" in req
        assert "age" not in req and "tag" not in req
        assert "required" not in get(got, "properties", "name")
        assert "required" not in get(got, "properties", "age")


def test_preserves_large_number_precision():
    # TestCleanJSONSchema_PreservesLargeNumberPrecision
    inp = J(r'''{"type": "object","properties": {"big_int": {"type": "integer","minimum": 9007199254740993},
        "bare_child": {"sub": { "type": "string" }}}}}'''.replace("}}}", "}}"))
    got = json.dumps(gsc.clean_antigravity_response(inp))
    assert "9007199254740993" in got, got


def test_bare_map_with_request_and_tools_names():
    # TestCleanJSONSchema_BarePropertyMapWithRequestAndToolsNames
    inp = {"type": "object", "properties": {"data": {"request": {
        "method": {"type": "string", "required": True}, "url": {"type": "string"}},
        "headers": {"authorization": {"type": "string"}},
        "tools": {"name": {"type": "string"}}}}}
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "data", "type") == "object"
        for p in ("headers", "tools", "request"):
            assert get(got, "properties", "data", "properties", p, "type") == "object", p
        assert get(got, "properties", "data", "properties", "request", "properties", "method", "type") == "string"
        assert "method" in get(got, "properties", "data", "properties", "request", "required")


def test_bare_map_with_sibling_description():
    # TestCleanJSONSchema_BarePropertyMapWithSiblingDescription
    inp = {"type": "object", "properties": {"data": {"description": "Task payload",
        "parent": {"type": "string", "required": True}, "insert_after": {"type": "string"}}}}
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "data", "type") == "object"
        assert get(got, "properties", "data", "description") == "Task payload"
        assert "parent" in get(got, "properties", "data", "required")


def test_single_key_schema_wrapper():
    # TestCleanJSONSchema_SingleKeySchemaWrapper
    inner = J(r'''{"type": "object","properties": {"data": {"parent": { "type": "string", "required": true }}}}}'''
              .replace("}}}", "}}"))
    got = validated_tool({"schema": inner})
    assert "schema" in got
    assert get(got, "schema", "properties", "data", "type") == "object"
    assert get(got, "schema", "properties", "data", "properties", "parent", "type") == "string"
    assert "parent" in get(got, "schema", "properties", "data", "required")


def test_bare_map_explicit_type_object():
    # TestCleanJSONSchema_BarePropertyMapWithExplicitTypeObject
    inp = {"type": "object", "properties": {"data": {"type": "object",
        "parent": {"type": "string", "required": True}, "insert_after": {"type": "string"}}}}
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "data", "properties", "parent", "type") == "string"
        assert "parent" in get(got, "properties", "data", "required")


def test_bare_map_with_nullable():
    # TestCleanJSONSchema_BarePropertyMapWithNullable
    inp = {"type": "object", "properties": {"data": {"nullable": True, "description": "Task payload",
        "parent": {"type": "string"}}}}
    for cleaner_name in ("antigravityResponse", "gemini"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "data", "type") == "object"
        assert get(got, "properties", "data", "properties", "parent", "type") == "string"


def test_html_characters_not_escaped():
    # TestCleanJSONSchema_PreservesHTMLCharactersWithoutEscaping
    inp = {"type": "object", "properties": {"data": {"description": "Uses <tag> & symbols > threshold",
        "parent": {"type": "string"}}}}
    got = json.dumps(gsc.clean_antigravity_response(inp), ensure_ascii=False)
    assert "<tag>" in got and "& symbols >" in got
    assert "\\u003c" not in got and "\\u003e" not in got and "\\u0026" not in got


def test_vendor_extension_on_enum_not_wrapped():
    # TestCleanJSONSchema_VendorExtensionOnEnumNotWrappedIntoProperties
    inp = J(r'''{"type": "string","enum": ["FOO", "BAR"],"x-google-enum-descriptions": {
        "FOO": "Foo option","BAR": "Bar option"}}''')
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert "properties" not in got, cleaner_name
        assert got["type"] == "string", cleaner_name


def test_object_default_not_wrapped():
    # TestCleanJSONSchema_ObjectDefaultNotWrappedIntoProperties
    inp = {"type": "object", "properties": {"settings": {"type": "object",
        "default": {"theme": "dark", "lang": "en"}}}}
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "settings", "properties", "default") is None, cleaner_name


def test_mixed_properties_and_orphan():
    # TestCleanJSONSchema_MixedPropertiesAndOrphanBareProperty
    inp = J(r'''{"type": "object","properties": {"foo": { "type": "string" }},
        "bar": {"type": "integer","required": true}}''')
    for cleaner_name in ("antigravity", "gemini", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "foo", "type") == "string"
        assert get(got, "properties", "bar", "type") == "integer"
        assert "bar" in got["required"]
        assert "bar" not in got


def test_additional_properties_schema_not_wrapped():
    # TestCleanJSONSchema_PreservesAdditionalPropertiesObjectSchema
    inp = J(r'''{"additionalProperties": {"type": "string"}}''')
    for cleaner_name in ("antigravityResponse", "gemini"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "additionalProperties") is None, cleaner_name


def test_response_anyof_required_only_branches():
    # TestCleanJSONSchemaForAntigravityResponse_AnyOfRequiredOnlyBranches (issue 5219 #2)
    inp = J(r'''{"type": "object","anyOf": [{"required": ["left"]},{"required": ["right"]}],
        "properties": {"left": {"type": "integer"},"right": {"type": "integer"}},
        "additionalProperties": false}''')
    got = gsc.clean_antigravity_response(inp)
    assert got["type"] == "object"
    assert "left" in got["properties"] and "right" in got["properties"]
    branches = got["anyOf"]
    assert len(branches) == 2
    for b, key in zip(branches, ("left", "right")):
        assert b["type"] == "object" and key in b["properties"] and b["required"] == [key]


def test_contains_keyword_stripped():
    # TestCleanJSONSchemaForAntigravityResponse_ContainsKeywordStripped (issue 5219 #3)
    inp = J(r'''{"type": "object","properties": {"tags": {"type": "array","items": {"type": "string"},
        "contains": {"enum": ["x"]}}},"required": ["tags"],"additionalProperties": false}''')
    for cleaner_name in ("antigravityResponse", "antigravity", "gemini"):
        got = CLEANERS[cleaner_name](inp)
        tags = get(got, "properties", "tags")
        assert "contains" not in tags, cleaner_name
        assert "contains" in tags.get("description", ""), cleaner_name


def test_draft04_identifier_keywords():
    # TestCleanJSONSchema_RemovesDraft04IdAndSchemaIdentifierKeywords (issue 5888)
    inp = J(r'''{"id": "http://example.com/root.json","$anchor": "rootAnchor",
        "$vocabulary": {"https://json-schema.org/draft/2020-12/vocab/core": true},"type": "object",
        "properties": {"kind": {"type": "string","enum": ["short", "video"],"id": "ContentType",
            "$anchor": "contentTypeAnchor","$dynamicAnchor": "dynAnchor","$dynamicRef": "#dynAnchor",
            "description": "Kind"},
          "id": {"type": "string","description": "Property legitimately named id should survive"}},
        "required": ["kind"]}''')
    for cleaner_name in ("gemini", "antigravity", "antigravityTool", "antigravityResponse"):
        got = CLEANERS[cleaner_name](inp)
        assert "id" not in got and "$anchor" not in got and "$vocabulary" not in got, cleaner_name
        kind = get(got, "properties", "kind")
        for kw in ("id", "$anchor", "$dynamicAnchor", "$dynamicRef"):
            assert kw not in kind, (cleaner_name, kw)
        assert get(got, "properties", "id") is not None, cleaner_name
        assert get(got, "properties", "id", "type") == "string", cleaner_name

    ref_input = J(r'''{"definitions": {"ContentType": {"type": "string","enum": ["short", "video"],
        "id": "ContentType","description": "Kind"}},"type": "object",
        "properties": {"kind": { "$ref": "#/definitions/ContentType" }},"required": ["kind"]}''')
    for cleaner_name in ("antigravity", "antigravityResponse"):
        got = CLEANERS[cleaner_name](ref_input)
        kind = get(got, "properties", "kind")
        assert kind and kind["type"] == "string", cleaner_name
        assert "id" not in kind, cleaner_name
        assert "definitions" not in got, cleaner_name


def test_true_boolean_subschemas():
    # TestCleanJSONSchema_TrueBooleanSubschemas (issue 3551)
    inp = J(r'''{"type": "object","properties": {"screenshot_id": true,"filename": true,
        "file_size": true,"source_file_checksum": true,"disabled_field": false},
        "additionalProperties": true}''')
    for cleaner_name in ("antigravity", "antigravityTool", "antigravityResponse", "gemini"):
        got = CLEANERS[cleaner_name](inp)
        for prop in ("screenshot_id", "filename", "file_size", "source_file_checksum"):
            node = get(got, "properties", prop)
            assert isinstance(node, dict) and node == {}, (cleaner_name, prop, node)
        assert get(got, "properties", "disabled_field") is False, cleaner_name


def test_nested_true_boolean_subschemas():
    # TestCleanJSONSchema_NestedTrueBooleanSubschemas
    inp = J(r'''{"type": "object","properties": {"tags": {"type": "array","items": true},
        "tuple": {"type": "array","items": [true, {"type": "string"}],"additionalItems": true},
        "union": {"anyOf": [true, {"type": "string"}]},
        "combination": {"allOf": [true, {"type": "object","properties": {"opt": true}}]},
        "metadata": {"type": "object","properties": {"nested_true": true,"nested_false": false}},
        "large_int": 9007199254740993},"$defs": {"custom_schema": true}}''')
    for cleaner_name in ("antigravity", "antigravityTool", "antigravityResponse", "gemini"):
        got = CLEANERS[cleaner_name](inp)
        tags_items = get(got, "properties", "tags", "items")
        assert tags_items is None or tags_items is not True, cleaner_name
        nt = get(got, "properties", "metadata", "properties", "nested_true")
        assert nt == {}, (cleaner_name, nt)
        assert get(got, "properties", "metadata", "properties", "nested_false") is False, cleaner_name
        tuple_items = get(got, "properties", "tuple", "items")
        assert tuple_items is None or (len(tuple_items) and tuple_items[0] is not True), cleaner_name
        opt = get(got, "properties", "combination", "properties", "opt")
        assert opt is None or opt is not True, cleaner_name
        assert got["properties"]["large_int"] == 9007199254740993, cleaner_name


def test_root_and_wrapped_true():
    # TestCleanJSONSchema_RootAndWrappedTrue
    for cleaner_name in ("antigravity", "antigravityTool", "antigravityResponse", "gemini"):
        assert CLEANERS[cleaner_name](True) == {}, cleaner_name
        got = CLEANERS[cleaner_name]({"schema": True})
        assert not isinstance(got.get("schema"), bool), (cleaner_name, got)


def test_gemini_json_schema_preserves_constraints():
    # TestCleanJSONSchemaForGeminiJSONSchema_PreservesAdditionalPropertiesAndPattern_Issue5959
    inp = J(r'''{"$schema": "https://json-schema.org/draft/2020-12/schema","title": "SubmitTool",
        "type": "object","additionalProperties": false,
        "properties": {"recipient": {"type": "string","pattern": "^(alice|bob)$","minLength": 3,"maxLength": 10},
          "amount": {"type": "number","minimum": 1},
          "nested": {"type": "object","additionalProperties": false,
                     "properties": {"tag": {"type": "string","pattern": "^[a-z]+$"}}},
          "items_list": {"type": "array","items": {"type": "string","pattern": "^[0-9]+$"}}},
        "required": ["recipient", "amount", "non_existent"]}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert got["additionalProperties"] is False
    assert get(got, "properties", "recipient", "pattern") == "^(alice|bob)$"
    assert get(got, "properties", "recipient", "minLength") == 3
    assert get(got, "properties", "recipient", "maxLength") == 10
    assert get(got, "properties", "amount", "minimum") == 1
    assert get(got, "properties", "nested", "additionalProperties") is False
    assert get(got, "properties", "nested", "properties", "tag", "pattern") == "^[a-z]+$"
    assert get(got, "properties", "items_list", "items", "pattern") == "^[0-9]+$"
    assert "title" not in got and "$schema" not in got
    assert got.get("description") != "No extra properties allowed"
    for p in ("recipient", "amount"):
        d = get(got, "properties", p, "description")
        assert not (d and "pattern:" in d)
    assert len(got["required"]) == 2

    legacy = gsc.clean_gemini(inp)
    assert "additionalProperties" not in json.dumps(legacy)
    assert "No extra properties allowed" in json.dumps(legacy)
    recipient = get(legacy, "properties", "recipient")
    assert "pattern" not in recipient and "minLength" not in recipient and "maxLength" not in recipient


def test_gemini_json_schema_preserves_schema_valued_ap():
    # TestCleanJSONSchemaForGeminiJSONSchema_PreservesSchemaValuedAdditionalProperties
    inp = J(r'''{"type": "object","additionalProperties": {"type": "string","pattern": "^[a-z]+$","minLength": 2}}''')
    got = gsc.clean_gemini_json_schema(inp)
    ap = got["additionalProperties"]
    assert isinstance(ap, dict) and ap["type"] == "string" and ap["pattern"] == "^[a-z]+$" and ap["minLength"] == 2
    assert "additionalProperties" not in json.dumps(gsc.clean_gemini(inp))


def test_array_items_require_array_type():
    # TestCleanJSONSchema_ArrayItemsRequireArrayType_Issue6011 (all 7 cases)
    cleaners = {"gemini": gsc.clean_gemini, "geminiJSONSchema": gsc.clean_gemini_json_schema,
                "antigravity": validated_tool, "antigravityLegacy": legacy_tool}
    missing_type = J(r'''{"type": "object","properties": {"revision_reasons": {"type": "array","items": {
        "type": "object","properties": {"evidence_reference": {"description": "references to evidence",
        "items": {"type": "string"}}}}}}}''')
    for name, clean in cleaners.items():
        got = clean(missing_type)
        target = get(got, "properties", "revision_reasons", "items", "properties", "evidence_reference")
        assert target is not None, name
        assert target["type"] == "array", name
        assert target["items"]["type"] == "string", name
        assert clean(got) == got, f"{name} not idempotent"

    union_type = J(r'''{"type": "object","properties": {"revision_reasons": {"type": "array","items": {
        "type": "object","properties": {"evidence_reference": {"type": ["string", "array"],
        "items": {"type": "string"}}}}}}}''')
    for name, clean in cleaners.items():
        got = clean(union_type)
        target = get(got, "properties", "revision_reasons", "items", "properties", "evidence_reference")
        assert target["type"] == "array", name

    non_array = J(r'''{"type": "object","properties": {"label": {"type": "string","items": { "type": "string" }},
        "config": {"type": "object","properties": { "key": { "type": "string" } },"items": { "type": "string" }}}}}'''
                   .replace("}}}", "}}"))
    for name, clean in cleaners.items():
        got = clean(non_array)
        assert get(got, "properties", "label", "items") is None, name
        assert get(got, "properties", "config", "items") is None, name
        assert get(got, "properties", "label", "type") == "string", name

    named_items = J(r'''{"type": "object","properties": {"items": {"type": "string",
        "description": "a field named items"}}}''')
    for name, clean in cleaners.items():
        got = clean(named_items)
        assert get(got, "properties", "items", "type") == "string", name

    root_missing = J(r'''{"items": {"type": "string"}}''')
    for name, clean in cleaners.items():
        got = clean(root_missing)
        assert got["type"] == "array", name
        assert got["items"]["type"] == "string", name

    resp = gsc.clean_antigravity_response(J(r'''{"type":"object","properties":{"names":{"items":{"type":"string"}}}}'''))
    assert get(resp, "properties", "names", "type") == "array", json.dumps(resp)
    resp2 = gsc.clean_antigravity_response(J(r'''{"type":"object","properties":{"values":{"type":"array"}}}'''))
    assert get(resp2, "properties", "values", "items") is None


def test_uppercase_array_type_items_preserved():
    # TestSanitizeArrayItems_PreservesItemsForUppercaseArrayType
    inp = J(r'''{"type": "OBJECT","properties": {"summary": {"type": "STRING"},
        "brands": {"type": "ARRAY","items": {"type": "STRING"}},
        "catalog": {"type": "OBJECT","properties": {"items": {"type": "ARRAY","items": {
            "type": "OBJECT","properties": {"id": {"type": "STRING"}}}}}}}}''')
    for cleaner_name in ("antigravityResponse", "antigravity", "gemini", "antigravityTool"):
        got = CLEANERS[cleaner_name](inp)
        assert get(got, "properties", "brands", "items") is not None, cleaner_name
        assert get(got, "properties", "brands", "type").lower() == "array", cleaner_name
        assert get(got, "properties", "catalog", "properties", "items", "items") is not None, cleaner_name


def test_enforces_object_type_for_properties():
    # TestCleanJSONSchema_EnforcesObjectTypeForProperties_Issue6394
    cleaners = {"gemini": gsc.clean_gemini, "geminiJSONSchema": gsc.clean_gemini_json_schema,
                "antigravityTool": legacy_tool, "antigravityResponse": gsc.clean_antigravity_response}
    cases = [
        J(r'''{"type":"object","properties":{"requestBody":{"type":"object","properties":{"delivery":{
            "type":"object","properties":{"endpoint":{"type":["string","object"],
            "properties":{"traces":{"type":"string"}},"required":["traces"]}},"required":["endpoint"]}},
            "required":["delivery"]}},"required":["requestBody"]}'''),
        J(r'''{"type":"object","properties":{"endpoint":{"properties":{"traces":{"type":"string"}},
            "required":["traces"],"anyOf":[{"type":"string"},{"type":"object",
            "properties":{"other":{"type":"string"}}}]}}}'''),
        J(r'''{"type":"object","properties":{"endpoint":{"type":"string","properties":{"traces":{"type":"string"}},
            "required":["traces"]}}}'''),
    ]
    for idx, inp in enumerate(cases):
        for name, clean in cleaners.items():
            got = clean(inp)
            if idx == 0:
                node = get(got, "properties", "requestBody", "properties", "delivery", "properties", "endpoint")
                assert node["type"].lower() == "object", (name, json.dumps(got))
                assert get(node, "properties", "traces") is not None, name
            elif idx == 1:
                node = get(got, "properties", "endpoint")
                assert node["type"].lower() == "object", (name, json.dumps(got))
                assert get(node, "properties", "traces") is not None, name
            else:
                node = get(got, "properties", "endpoint")
                assert node["type"].lower() == "object", (name, json.dumps(got))


def test_removes_dependent_keywords():
    # TestCleanJSONSchema_RemovesDependentKeywords_Issue6539
    inp = J(r'''{"type": "object","properties": {"baseline_url": {"type": "string"},
        "candidate_url": {"type": "string"},
        "options": {"type": "object","properties": {"threshold": {"type": "number"},"diff_color": {"type": "string"}},
            "dependentRequired": {"threshold": ["diff_color"]},
            "dependentSchemas": {"threshold": {"properties": {"diff_color": {"type": "string"}}}},
            "dependencies": {"threshold": ["diff_color"]}},
        "dependentRequired": {"type": "string","description": "Legitimate property named dependentRequired"},
        "dependentSchemas": {"type": "object","properties": {"nested": {"type": "string"}},
            "dependentRequired": {"nested": ["other"]}},
        "dependencies": {"type": "string","description": "Legitimate property named dependencies"}},
        "dependentRequired": {"baseline_url": ["candidate_url"]},
        "dependentSchemas": {"baseline_url": {"required": ["candidate_url"]}},
        "dependencies": {"baseline_url": ["candidate_url"]}}''')
    cleaners = {"antigravity": validated_tool, "antigravityTool": legacy_tool,
                "antigravityResponse": gsc.clean_antigravity_response,
                "gemini": gsc.clean_gemini, "geminiJSONSchema": gsc.clean_gemini_json_schema}
    for name, clean in cleaners.items():
        got = clean(inp)
        blob = json.dumps(got)
        for kw in ("dependentRequired", "dependentSchemas", "dependencies"):
            assert kw not in got, (name, kw, blob)
            assert get(got, "properties", "options", kw) is None, (name, kw)
        assert get(got, "properties", "dependentSchemas", "dependentRequired") is None, name
        for prop in ("dependentRequired", "dependentSchemas", "dependencies"):
            assert get(got, "properties", prop) is not None, (name, prop)


def test_preserves_multi_branch_unions():
    # TestCleanJSONSchema_PreservesMultiBranchAnyOfOneOf_Issue3662 (preserve side)
    inp = J(r'''{"type": "object","properties": {"item": {"anyOf": [
        { "type": "string" },
        {"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
        {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]}]}},"required": ["item"]}''')
    for cleaner_name in ("antigravity", "antigravityTool", "antigravityResponse",
                         "gemini", "geminiJSONSchema"):
        got = CLEANERS[cleaner_name](inp)
        assert got == inp, (cleaner_name, json.dumps(got))
        assert CLEANERS[cleaner_name](got) == got, cleaner_name


def test_nullable_multibranch_union():
    # .../NullableMultiBranchUnionStripsNullBranchAndSetsNullable
    nullable_union = J(r'''{"type": "object","properties": {"item": {"anyOf": [
        { "type": "string" },
        {"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
        {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]},
        { "type": "null" }]}},"required": ["item"]}''')
    got = validated_tool(nullable_union)
    branches = get(got, "properties", "item", "anyOf")
    assert len(branches) == 3
    assert get(got, "properties", "item", "nullable") is True
    assert get(branches[2], "properties", "audio") is not None

    gj = gsc.clean_gemini_json_schema(nullable_union)
    assert len(get(gj, "properties", "item", "anyOf")) == 3
    assert get(gj, "properties", "item", "nullable") is None
    assert "(nullable)" in get(gj, "properties", "item", "description")

    for nested in (J(r'''{"type":"object","properties":{"item":{"anyOf":[{"type":"null"},
            {"anyOf":[{"type":"string"},{"type":"integer"}]}]}}}'''),
                   J(r'''{"type":"object","properties":{"item":{"anyOf":[
            {"anyOf":[{"type":"string"},{"type":"integer"}]},{"type":"null"}]}}}''')):
        exp = J(r'''{"type": "object","properties": {"item": {"anyOf": [
            { "type": "string" }, { "type": "integer" }],"nullable": true,"description": "(nullable)"}}}''')
        once = validated_tool(nested)
        assert once == exp, json.dumps(once)
        assert validated_tool(once) == exp


def test_oneof_normalized_on_native_carriers():
    # .../OneOfNormalizedToAnyOfOnNativeSchemaCarriers
    one_of_input = J(r'''{"type": "object","properties": {"item": {"oneOf": [
        { "type": "string" },
        {"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
        {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]}]}},"required": ["item"]}''')
    for cleaner_name in ("antigravity", "antigravityTool", "antigravityResponse", "gemini"):
        got = CLEANERS[cleaner_name](one_of_input)
        item = get(got, "properties", "item")
        assert "oneOf" not in item, cleaner_name
        assert len(item["anyOf"]) == 3, cleaner_name
    gj = gsc.clean_gemini_json_schema(one_of_input)
    assert len(get(gj, "properties", "item", "oneOf")) == 3


def test_required_only_branch_materialization():
    # .../PreservesUnionAndBranchRequiredWhenParentHasProperties
    parent_props_union = J(r'''{"type": "object","properties": {},"anyOf": [
        {"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
        {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]}]}''')
    for name in ("antigravityTool", "antigravityResponse", "gemini", "geminiJSONSchema"):
        got = CLEANERS[name](parent_props_union)
        if name in ("antigravityTool", "gemini"):
            assert got == parent_props_union, (name, json.dumps(got))
        elif name == "geminiJSONSchema":
            assert got == parent_props_union, (name, json.dumps(got))
        else:
            assert got == parent_props_union, (name, json.dumps(got))

    mixed = J(r'''{"type": "object","properties": {"image": { "type": "string" },
        "audio": { "type": "string" },"text": { "type": "string" }},"anyOf": [
        {"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
        {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]},
        {"required": ["text"]}]}''')
    exp_mixed = J(r'''{"type": "object","properties": {"image": { "type": "string" },
        "audio": { "type": "string" },"text": { "type": "string" }},"anyOf": [
        {"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
        {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]},
        {"type": "object","properties": {"text": { "type": "string" }},"required": ["text"]}]}''')
    assert validated_tool(mixed) == exp_mixed

    pure = J(r'''{"type": "object","properties": {"image": { "type": "string" },"audio": { "type": "string" }},
        "anyOf": [{ "required": ["image"] },{ "required": ["audio"] }]}''')
    exp_pure = J(r'''{"type": "object","properties": {"image": { "type": "string" },"audio": { "type": "string" }},
        "anyOf": [{"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
                   {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]}]}''')
    assert validated_tool(pure) == exp_pure


def test_boolean_union_branches_and_siblings():
    # .../BooleanUnionBranchesAndSiblingAnyOfOneOf
    assert claude_tool(J(r'''{"type":"object","properties":{"value":{"anyOf":[false,{"type":"string"}]}}}''')) \
        == J(r'''{"type":"object","properties":{"value":{"type":"string"}}}''')
    assert validated_tool(J(r'''{"type":"object","properties":{"value":{"anyOf":[false,{"type":"string"}]}}}''')) \
        == J(r'''{"type":"object","properties":{"value":{"type":"string"}}}''')
    assert validated_tool(J(r'''{"type":"object","properties":{"value":{"oneOf":[false,{"type":"string"},{"type":"integer"}]}}}''')) \
        == J(r'''{"type":"object","properties":{"value":{"anyOf":[{"type":"string"},{"type":"integer"}]}}}''')
    assert legacy_tool(J(r'''{"type":"object","properties":{"value":{"anyOf":[true,{"type":"string"},{"type":"integer"}]}}}''')) \
        == J(r'''{"type":"object","properties":{"value":{"anyOf":[{},{"type":"string"},{"type":"integer"}]}}}''')

    sibling_unions = J(r'''{"type": "object","properties": {"value": {"anyOf": [
        { "type": "string" },{ "type": "integer" }],"oneOf": [{ "type": "string" },{ "type": "boolean" }]}}}''')
    for cleaner_name in ("antigravity", "antigravityTool", "antigravityResponse", "gemini"):
        got = CLEANERS[cleaner_name](sibling_unions)
        value = get(got, "properties", "value")
        assert "oneOf" not in value, cleaner_name
        assert len(value["anyOf"]) == 2, cleaner_name
        assert "oneOf:" in value["description"], cleaner_name
    gj = gsc.clean_gemini_json_schema(sibling_unions)
    assert len(get(gj, "properties", "value", "anyOf")) == 2
    assert len(get(gj, "properties", "value", "oneOf")) == 2


def test_claude_flattens_nested_unions():
    # .../AntigravityClaudeToolFlattensNestedUnions (issue 3662 follow-up)
    input_ = J(r'''{"type": "object","properties": {"item": {"anyOf": [
        { "type": "string" },
        {"type": "object","properties": {"image": { "type": "string" }},"required": ["image"]},
        {"type": "object","properties": {"audio": { "type": "string" }},"required": ["audio"]}]}},"required": ["item"]}''')
    flat = claude_tool(input_)
    item = get(flat, "properties", "item")
    assert "anyOf" not in item and "oneOf" not in item
    assert item["type"] == "object"
    assert get(item, "properties", "image") is not None
    assert "Accepts:" in item["description"] and "string" in item["description"]
    assert claude_tool(flat) == flat
    assert claude_tool(validated_tool(input_)) == flat
    assert validated_tool(flat) == flat

    nullable_union_input = J(r'''{"type":"object","properties":{"value":{"anyOf":[
        {"type":"string"},{"type":"integer"},{"type":"null"}]}}}''')
    direct = claude_tool(nullable_union_input)
    dv = get(direct, "properties", "value")
    assert dv["nullable"] is True and dv["type"] == "string", json.dumps(direct)
    chained = claude_tool(validated_tool(nullable_union_input))
    cv = get(chained, "properties", "value")
    assert cv["nullable"] is True and cv["type"] == "string" and "anyOf" not in cv, json.dumps(chained)
    assert claude_tool(chained) == chained

    assert claude_tool(J(r'''{"type":"object","properties":{"value":{"anyOf":[true,{"type":"string"},{"type":"integer"}]}}}''')) \
        == J(r'''{"type":"object","properties":{"value":{"type":"string","description":"Accepts: string | integer"}}}''')
    assert claude_tool(J(r'''{"type":"object","properties":{"value":{"oneOf":[false,{"type":"string"},{"type":"integer"}]}}}''')) \
        == J(r'''{"type":"object","properties":{"value":{"type":"string","description":"Accepts: string | integer"}}}''')

    sibling = J(r'''{"type": "object","properties": {"value": {"anyOf": [
        { "type": "string" },{ "type": "integer" }],"oneOf": [{ "type": "string" },{ "type": "boolean" }]}}}''')
    sc = claude_tool(sibling)
    sv = get(sc, "properties", "value")
    assert "anyOf" not in sv and "oneOf" not in sv
    assert sv == J(r'''{"type":"string","description":"Accepts: string | boolean"}'''), json.dumps(sc)
    assert claude_tool(sc) == sc


def test_null_only_unions_do_not_panic():
    # Null-only unions across every cleaner must stay valid with type null.
    for schema in (J(r'''{"type":"object","properties":{"value":{"anyOf":[{"type":"null"}]}}}'''),
                   J(r'''{"type":"object","properties":{"value":{"oneOf":[{"type":"null"},{"type":"null"}]}}}'''),
                   J(r'''{"type":"object","properties":{"value":{"anyOf":[false,{"type":"null"}],"description":"Null only"}}}''')):
        for name, clean in CLEANERS.items():
            got = clean(schema)
            json.dumps(got)
            assert get(got, "properties", "value", "type") == "null", (name, json.dumps(got))


def test_removes_discriminator():
    # TestCleanJSONSchema_RemovesDiscriminatorKeyword_Issue6639
    inp = J(r'''{"type": "object","properties": {"target": {"anyOf": [
            {"type": "object", "properties": {"kind": {"const": "a"}, "url": {"type": "string"}}},
            {"type": "object", "properties": {"kind": {"const": "b"}, "path": {"type": "string"}}}],
            "discriminator": {"propertyName": "kind"}},
        "deliverables": {"type": "array","items": {"anyOf": [
            {"type": "object", "properties": {"kind": {"const": "file"}, "path": {"type": "string"}}},
            {"type": "object", "properties": {"kind": {"const": "link"}, "url": {"type": "string"}}}],
            "discriminator": {"propertyName": "kind"}}},
        "polymorphicObject": {"type": "object","properties": {"kind": {"type": "string"}},
            "oneOf": [{"properties": {"a": {"type": "string"}}},{"properties": {"b": {"type": "number"}}}],
            "discriminator": {"propertyName": "kind","mapping": {"a": "#/definitions/A", "b": "#/definitions/B"}}},
        "discriminator": {"type": "string","description": "Legitimate property named discriminator"},
        "nestedDiscriminatorProp": {"type": "object","properties": {"discriminator": {"type": "object",
            "properties": {"kind": {"type": "string"}},"discriminator": {"propertyName": "kind"}}}}},
        "discriminator": {"propertyName": "target"}}''')
    for name, clean in CLEANERS.items():
        got = clean(inp)
        blob = json.dumps(got)
        assert get(got, "discriminator") is None, (name, blob)
        assert get(got, "properties", "target", "discriminator") is None, name
        assert get(got, "properties", "deliverables", "items", "discriminator") is None, name
        assert get(got, "properties", "polymorphicObject", "discriminator") is None, name
        assert get(got, "properties", "nestedDiscriminatorProp", "properties",
                   "discriminator", "discriminator") is None, name
        if name != "antigravityClaude":
            assert len(get(got, "properties", "target", "anyOf")) == 2, name
            assert len(get(got, "properties", "deliverables", "items", "anyOf")) == 2, name
        assert get(got, "properties", "discriminator") is not None, name
        assert get(got, "properties", "nestedDiscriminatorProp", "properties",
                   "discriminator", "properties", "kind") is not None, name


def test_removes_content_media_type():
    # TestCleanJSONSchema_RemovesContentMediaTypeKeyword_Issue6699
    inp = J(r'''{"type": "object","contentMediaType": "application/json","contentEncoding": "utf-8",
        "properties": {"value": {"anyOf": [{"type": "object","properties": {}},
            {"type": "string","contentMediaType": "application/json","contentEncoding": "base64"}]},
        "directPayload": {"type": "string","contentMediaType": "application/json","contentEncoding": "base64"},
        "contentMediaType": {"type": "string","contentMediaType": "text/plain",
                             "description": "Legitimate property named contentMediaType"},
        "contentEncoding": {"type": "string","contentEncoding": "base64",
                            "description": "Legitimate property named contentEncoding"}}}''')
    for name, clean in CLEANERS.items():
        got = clean(inp)
        blob = json.dumps(got)
        assert get(got, "contentMediaType") is None and get(got, "contentEncoding") is None, (name, blob)
        branches = get(got, "properties", "value", "anyOf")
        if name != "antigravityClaude":
            assert len(branches) == 2, name
            assert "contentMediaType" not in json.dumps(branches[1]), name
        assert get(got, "properties", "directPayload", "contentMediaType") is None, name
        assert get(got, "properties", "contentMediaType", "contentMediaType") is None, name
        assert get(got, "properties", "contentEncoding", "contentEncoding") is None, name
        assert get(got, "properties", "contentMediaType") is not None, name
        assert get(got, "properties", "contentEncoding") is not None, name


def test_removes_readonly_writeonly():
    # TestCleanJSONSchema_RemovesReadOnlyWriteOnlyKeywords_Issue6778
    inp = J(r'''{"type": "object","$schema": "http://json-schema.org/draft-04/schema#","readOnly": false,
        "writeOnly": false,"properties": {"parent": {"type": "string","description": "Parent resource name"},
        "database": {"type": "object","properties": {
            "name": {"type": "string","description": "Database resource name","readOnly": true},
            "uid": {"type": "string","readOnly": true},"createTime": {"type": "string","readOnly": true},
            "etag": {"type": "string","writeOnly": true},"locationId": {"type": "string"}}},
        "itemsList": {"type": "array","items": {"type": "string","readOnly": true,"writeOnly": false}},
        "unionField": {"anyOf": [{"type": "string","readOnly": true},{"type": "integer","writeOnly": true}]},
        "readOnly": {"type": "boolean","readOnly": true,"description": "Legitimate property named readOnly"},
        "writeOnly": {"type": "boolean","writeOnly": true,"description": "Legitimate property named writeOnly"}},
        "required": ["parent"]}''')
    for name, clean in CLEANERS.items():
        got = clean(inp)
        blob = json.dumps(got)
        assert get(got, "readOnly") is None and get(got, "writeOnly") is None, (name, blob)
        for path in [("properties", "database", "properties", "name", "readOnly"),
                     ("properties", "database", "properties", "uid", "readOnly"),
                     ("properties", "database", "properties", "etag", "writeOnly"),
                     ("properties", "itemsList", "items", "readOnly"),
                     ("properties", "unionField", "readOnly"),
                     ("properties", "unionField", "anyOf", "0", "readOnly"),
                     ("properties", "readOnly", "readOnly"),
                     ("properties", "writeOnly", "writeOnly")]:
            assert get(got, *path) is None, (name, path)
        for kept in ("name", "uid", "createTime", "etag", "locationId"):
            assert get(got, "properties", "database", "properties", kept, "type") == "string", (name, kept)
        assert get(got, "properties", "database", "properties", "name", "description") == "Database resource name"
        assert get(got, "properties", "readOnly", "type") == "boolean", name
        assert get(got, "properties", "readOnly", "description") == "Legitimate property named readOnly", name
        assert get(got, "properties", "writeOnly", "type") == "boolean", name


# --- gemini_schema_ref_test.go goldens ---------------------------------------------

def _node_at(node, path):
    if path == "":
        return node
    cur = node
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return None
    return cur


def test_issue6897_ref_sibling_conjunctions():
    # TestIssue6897_RefSiblingConjunctions (representative cases, nested + root-ref)
    cases = [
        ({"type": "string", "minLength": 5}, '"minLength":1',
         {"allOf.0.minLength": 5, "minLength": 1}),
        ({"type": "string", "minLength": 1}, '"minLength":5',
         {"allOf.0.minLength": 1, "minLength": 5}),
        ({"type": "string"}, '"type":"number"', {"allOf.0.type": "string", "type": "number"}),
        ({"type": ["string", "number"]}, '"type":"number"',
         {"allOf.0.type": ["string", "number"], "type": "number"}),
        ({"type": "string", "enum": ["a", "b"]}, '"enum":["b","c"]',
         {"allOf.0.enum": ["a", "b"], "enum": ["b", "c"]}),
        ({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a"]},
         '"required":["b"]', {"allOf.0.required": ["a"], "required": ["b"], "allOf.0.properties.b.type": "string"}),
        ({"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
         '"properties":{"b":{"type":"string"}},"additionalProperties":false',
         {"allOf.0.properties": {"a": {"type": "string"}}, "properties": {"b": {"type": "string"}},
          "allOf.0.additionalProperties": False, "additionalProperties": False}),
        ({"type": "string", "allOf": [{"minLength": 5}]}, '"allOf":[{"maxLength":8}]',
         {"allOf.0.allOf.0.minLength": 5, "allOf.1.maxLength": 8}),
    ]
    for target, sibling_raw, checks in cases:
        for root_ref in (False, True):
            if root_ref:
                inp = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                       "$defs": {"A": target}, "$ref": "#/$defs/A"}
                path = ""
            else:
                inp = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                       "$defs": {"A": target}, "type": "object", "properties": {"x": {"$ref": "#/$defs/A"}}}
                path = "properties.x"
            # splice the sibling via raw json text
            import re as _re
            blob = json.dumps(inp)
            if root_ref:
                blob2 = blob.replace('"$ref": "#/$defs/A"}', '"$ref": "#/$defs/A", %s}' % sibling_raw)
            else:
                blob2 = blob.replace('"$ref": "#/$defs/A"}}', '"$ref": "#/$defs/A", %s}}' % sibling_raw)
            inp2 = J(blob2)
            for cleaner_name in ("inline", "gemini"):
                clean = gsc.inline_local_refs if cleaner_name == "inline" else gsc.clean_gemini_json_schema
                got = clean(inp2)
                if cleaner_name == "inline":
                    node = _node_at(got, path)
                else:
                    node = _node_at(_strip_meta(got), path)
                assert node is not None, (target, sibling_raw, root_ref, cleaner_name)
                for k, want in checks.items():
                    actual = _node_at(node, k)
                    assert actual is not None, (k, target, sibling_raw, root_ref, cleaner_name, json.dumps(node))
                    assert actual == want, (k, actual, want, root_ref, cleaner_name)
                if cleaner_name == "gemini":
                    assert clean(got) == got, (target, sibling_raw, "idempotence")


def _strip_meta(node):
    if isinstance(node, dict):
        return {k: v for k, v in node.items() if k not in ("$schema", "$defs", "definitions")}
    return node


def test_issue6897_annotations_and_identical_types():
    # TestIssue6897_RefSiblingAnnotationsAndIdenticalTypes
    inp = J(r'''{"$defs":{"A":{"type":"string","minLength":5,"description":"base","default":"base","examples":["base"]}},
        "properties":{"x":{"$ref":"#/$defs/A","type":"string","description":"local","default":"local",
        "examples":[{"$ref":"literal"}]}}}''')
    exp = J(r'''{"type":"string","minLength":5,"description":"local","default":"local","examples":[{"$ref":"literal"}]}''')
    assert _node_at(gsc.inline_local_refs(inp), "properties.x") == exp
    assert _node_at(gsc.clean_gemini_json_schema(inp), "properties.x") == exp


def test_issue6897_wrapped_ref_conjunction():
    # TestIssue6897_WrappedRefConjunction
    inp = J(r'''{"schema":{"$defs":{"A":{"type":"string","minLength":5}},"$ref":"#/$defs/A","minLength":1}}''')
    assert gsc.clean_gemini_json_schema(inp) == J(
        r'''{"schema":{"minLength":1,"allOf":[{"type":"string","minLength":5}]}}''')


def test_issue6897_root_ref_keeps_declarations():
    # TestIssue6897_RootRefKeepsDocumentDeclarations
    inp = J(r'''{"$schema":"https://json-schema.org/draft/2020-12/schema","$defs":{"A":{"type":"string","minLength":5}},
        "$ref":"#/$defs/A","minLength":1}''')
    assert gsc.inline_local_refs(inp) == J(
        r'''{"$schema":"https://json-schema.org/draft/2020-12/schema","$defs":{"A":{"type":"string","minLength":5}},
        "minLength":1,"allOf":[{"type":"string","minLength":5}]}''')


def test_issue6897_required_outside_allof():
    # TestIssue6897_RequiredOutsideAllOf
    inp = J(r'''{"type":"object","allOf":[{"properties":{"x":{"type":"string"}}}],"required":["x"]}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert got["required"][0] == "x", json.dumps(got)


def test_issue6897_array_conjunction_does_not_invent_items():
    # TestIssue6897_ArrayConjunctionDoesNotInventItems
    for root_ref in (False, True):
        if root_ref:
            inp = J(r'''{"$defs":{"A":{"type":"array","items":{"type":"integer"},"minItems":2}},
                "$ref":"#/$defs/A","type":"array","minItems":1}''')
            path = ""
        else:
            inp = J(r'''{"$defs":{"A":{"type":"array","items":{"type":"integer"},"minItems":2}},
                "type":"object","properties":{"x":{"$ref":"#/$defs/A","type":"array","minItems":1}}}''')
            path = "properties.x"
        got = gsc.clean_gemini_json_schema(inp)
        node = _node_at(got, path)
        assert node == J(r'''{"type":"array","minItems":1,"allOf":[
            {"type":"array","items":{"type":"integer"},"minItems":2}]}'''), json.dumps(node)
        assert got == gsc.clean_gemini_json_schema(got)


def test_issue6897_array_target_does_not_invent_items():
    # TestIssue6897_ArrayTargetDoesNotInventItemsBeforeInlining
    for root_ref in (False, True):
        if root_ref:
            inp = J(r'''{"$defs":{"A":{"type":"array","minItems":2}},"$ref":"#/$defs/A","minItems":1,
                "items":{"type":"integer"}}''')
            path = ""
        else:
            inp = J(r'''{"$defs":{"A":{"type":"array","minItems":2}},"type":"object",
                "properties":{"x":{"$ref":"#/$defs/A","minItems":1,"items":{"type":"integer"}}}}''')
            path = "properties.x"
        got = gsc.clean_gemini_json_schema(inp)
        node = _node_at(got, path)
        assert node == J(r'''{"minItems":1,"items":{"type":"integer"},"allOf":[
            {"type":"array","minItems":2}]}'''), json.dumps(node)
        assert got == gsc.clean_gemini_json_schema(got)


def test_issue6897_array_items_inside_allof():
    # TestIssue6897_ArrayItemsInsideAllOf
    inp = J(r'''{"type":"array","allOf":[{"items":{"type":"integer"}}]}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert "items" not in got, json.dumps(got)
    assert _node_at(got, "allOf.0.items.type") == "integer"


def test_issue6897_union_cannot_replace_conjunction():
    # TestIssue6897_UnionCannotReplaceConjunction
    inp = J(r'''{"type":"object","allOf":[{"properties":{"x":{"type":"string"}},"required":["x"]}],
        "anyOf":[{"type":"object"}]}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert _node_at(got, "allOf.0.properties.x.type") == "string"
    assert _node_at(got, "allOf.0.required.0") == "x"
    assert got == gsc.clean_gemini_json_schema(got)


def test_issue6897_conjunction_preserves_required_only_union():
    # TestIssue6897_ConjunctionPreservesRequiredOnlyUnion
    inp = J(r'''{"type":"object","allOf":[{"properties":{"a":{"type":"string"},"b":{"type":"string"}}},
        {"anyOf":[{"required":["a"]},{"required":["b"]}]}]}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert _node_at(got, "allOf.1.anyOf.0.required.0") == "a"
    assert _node_at(got, "allOf.1.anyOf.1.required.0") == "b"
    assert got == gsc.clean_gemini_json_schema(got)


def test_issue6897_ref_sibling_required_only_union():
    # TestIssue6897_RefSiblingRequiredOnlyUnion
    inp = J(r'''{"$defs":{"A":{"type":"object","properties":{"a":{"type":"string"},"b":{"type":"string"}},
        "minProperties":1}},"type":"object","properties":{"x":{"$ref":"#/$defs/A","minProperties":0,
        "anyOf":[{"required":["a"]},{"required":["b"]}]}}}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert _node_at(got, "properties.x.anyOf.0.required.0") == "a"
    assert _node_at(got, "properties.x.anyOf.1.required.0") == "b"
    assert got == gsc.clean_gemini_json_schema(got)


def test_issue6897_conjunction_does_not_infer_container_type():
    # TestIssue6897_ConjunctionDoesNotInferContainerType
    for sibling_key, sibling in (("items", {"type": "integer"}),
                                 ("properties", {"ignored": {"type": "integer"}})):
        a = {"type": "number", "minimum": 5}
        x = {"$ref": "#/$defs/A", "minimum": 1, sibling_key: sibling}
        inp = {"$defs": {"A": a}, "type": "object", "properties": {"x": x}}
        got = gsc.clean_gemini_json_schema(inp)
        node = get(got, "properties", "x")
        assert "type" not in node, sibling_key
        assert _node_at(node, "allOf.0.type") == "number", sibling_key
        assert got == gsc.clean_gemini_json_schema(got)


def test_issue6897_outer_union_preserves_conjunction():
    # TestIssue6897_OuterUnionPreservesConjunction
    cases = [
        (J(r'''{"type":"object","properties":{"x":{"type":"string"}},"anyOf":[
            {"allOf":[{"properties":{"x":{"minLength":5}}}]}]}'''),
         "anyOf.0.allOf.0.properties.x.minLength", "5"),
        (J(r'''{"$defs":{"A":{"type":"object","properties":{"x":{"minLength":5}},"minProperties":1}},
            "type":"object","properties":{"x":{"type":"string"}},"anyOf":[{"$ref":"#/$defs/A","minProperties":0}]}'''),
         "anyOf.0.allOf.0.properties.x.minLength", "5"),
        (J(r'''{"required":["x"],"anyOf":[{"allOf":[{"properties":{"x":{"type":"string"}}}]}]}'''),
         "required.0", "x"),
    ]
    for inp, path, want in cases:
        got = gsc.clean_gemini_json_schema(inp)
        actual = _node_at(got, path)
        assert _to_str(actual) == want, (path, json.dumps(got))
        assert got == gsc.clean_gemini_json_schema(got)


def _to_str(v):
    return json.dumps(v) if not isinstance(v, str) else v


def test_issue6897_ref_sibling_unevaluated_scope():
    # TestIssue6897_RefSiblingUnevaluatedScope
    cases = [
        ({"type": "object", "unevaluatedProperties": False}, {"properties": {"x": {"type": "string"}}},
         {"properties": {"x": {"type": "string"}},
          "allOf": [{"type": "object", "unevaluatedProperties": False}]}),
        ({"type": "array", "unevaluatedItems": False}, {"prefixItems": [{"type": "integer"}]},
         {"prefixItems": [{"type": "integer"}],
          "allOf": [{"type": "array", "unevaluatedItems": False}]}),
        ({"type": "object", "properties": {"x": {"type": "string"}}, "minProperties": 1},
         {"minProperties": 0, "unevaluatedProperties": False},
         {"minProperties": 0, "unevaluatedProperties": False,
          "allOf": [{"type": "object", "properties": {"x": {"type": "string"}}, "minProperties": 1}]}),
        ({"type": "array", "prefixItems": [{"type": "integer"}], "minItems": 1},
         {"minItems": 0, "unevaluatedItems": False},
         {"minItems": 0, "unevaluatedItems": False,
          "allOf": [{"type": "array", "prefixItems": [{"type": "integer"}], "minItems": 1}]}),
    ]
    for target, sibling, want in cases:
        for root_ref in (False, True):
            if root_ref:
                inp = {"$defs": {"A": target}, "$ref": "#/$defs/A"}
                inp.update(sibling)
                path = ""
            else:
                inp = {"$defs": {"A": target}, "properties": {"value": {"$ref": "#/$defs/A"}}}
                inp["properties"]["value"].update(sibling)
                path = "properties.value"
            got = gsc.inline_local_refs(inp)
            node = _node_at(got, path)
            node = {k: v for k, v in node.items() if k != "$defs"}
            assert node == want, (root_ref, json.dumps(node), json.dumps(want))


def test_issue6897_effective_min_length():
    # TestIssue6897_RefSiblingMinLength
    inp = J(r'''{"$schema":"https://json-schema.org/draft/2020-12/schema","$defs":{"A":{"type":"string","minLength":5}},
        "type":"object","properties":{"x":{"$ref":"#/$defs/A","minLength":1}}}''')
    got = gsc.clean_gemini_json_schema(inp)

    def eff_min(schema):
        m = schema.get("minLength") or 0
        for branch in schema.get("allOf", []):
            m = max(m, eff_min(branch))
        return m

    assert eff_min(get(got, "properties", "x")) == 5, json.dumps(got)


# --- issue 6732: ref inlining under cleaning -----------------------------------------

def _diamond_input(levels=12):
    defs = {"leaf0": {"type": "string", "description": "leaf leaf leaf leaf leaf leaf leaf leaf leaf leaf"}}
    for i in range(levels + 1):
        target = "leaf0" if i == 0 else "D%d" % (i - 1)
        defs["D%d" % i] = {"type": "object", "properties": {
            "a": {"$ref": "#/$defs/" + target}, "b": {"$ref": "#/$defs/" + target}}}
    return {"type": "object", "properties": {"leaf": {"$ref": "#/$defs/D%d" % levels}}, "$defs": defs}


def test_issue6732_gemini_inline_defs():
    # TestIssue6732_GeminiCleanersInlineLocalDefs
    inp = J(r'''{"type":"object","properties":{"person":{"$ref":"#/$defs/Person"}},"required":["person"],
        "$defs":{"Person":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}}''')
    for name in ("gemini", "geminiJSONSchema"):
        got = CLEANERS[name](inp)
        assert get(got, "properties", "person", "properties", "name", "type") == "string", name
        assert "$defs" not in got, name
        assert "See: Person" not in json.dumps(got), name


def test_issue6732_property_reuse():
    # TestIssue6732_GeminiCleanersInlinePropertyReuse
    inp = J(r'''{"type":"object","properties":{"billing":{"type":"object","properties":{"zip":{"type":"string"}}},
        "shipping":{"$ref":"#/properties/billing"}}}''')
    for name in ("gemini", "geminiJSONSchema"):
        got = CLEANERS[name](inp)
        assert get(got, "properties", "shipping", "properties", "zip", "type") == "string", name


def test_issue6732_shared_def_twice():
    # TestIssue6732_GeminiCleanersReuseSameDefinitionTwice
    inp = J(r'''{"type":"object","properties":{"billing":{"$ref":"#/$defs/Address"},
        "shipping":{"$ref":"#/$defs/Address"}},"$defs":{"Address":{"type":"object",
        "properties":{"zip":{"type":"string"}}}}}''')
    for name in ("gemini", "geminiJSONSchema"):
        got = CLEANERS[name](inp)
        for f in ("billing", "shipping"):
            assert get(got, "properties", f, "properties", "zip", "type") == "string", (name, f)


def test_issue6732_wrapped_resolves_inner_root():
    # TestIssue6732_WrappedSchemaResolvesAgainstInnerRoot
    inner = J(r'''{"type":"object","properties":{"person":{"$ref":"#/$defs/Person"}},"required":["person"],
        "$defs":{"Person":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}}''')
    got = legacy_tool({"schema": inner})
    assert get(got, "schema", "properties", "person", "properties", "name", "type") == "string"
    assert get(got, "schema", "$defs") is None
    assert "See: Person" not in json.dumps(got)


def test_issue6732_default_literal_preserved():
    # TestIssue6732_DefaultLiteralPreservedOnJSONSchemaRoute
    inp = J(r'''{"type":"object","properties":{"report":{"type":"object",
        "default":{"title":"Report","format":"pdf","id":7,"x-owner":"ops"}}}}''')
    got = gsc.clean_gemini_json_schema(inp)
    d = get(got, "properties", "report", "default")
    assert d == {"title": "Report", "format": "pdf", "id": 7, "x-owner": "ops"}, json.dumps(got)


def test_issue6732_example_literals_preserved():
    # TestIssue6732_ExampleLiteralsPreserved
    inp = J(r'''{"type":"object","properties":{"a":{"type":"string","example":{"title":"Hi","x-owner":"ops","id":3},
        "examples":[{"title":"Hi2","x-owner":"ops","id":4}]}}}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert get(got, "properties", "a", "example") == {"title": "Hi", "x-owner": "ops", "id": 3}
    assert get(got, "properties", "a", "examples")[0] == {"title": "Hi2", "x-owner": "ops", "id": 4}


def test_issue6732_inline_leaves_literal_refs():
    # TestIssue6732_InlineLocalRefsLeavesLiteralRefs
    inp = J(r'''{"type":"object","properties":{"a":{"type":"object","default":{"$ref":"#/$defs/Person"}}},
        "$defs":{"Person":{"type":"object","properties":{"name":{"type":"string"}}}}}''')
    got = gsc.inline_local_refs(inp)
    assert get(got, "properties", "a", "default", "$ref") == "#/$defs/Person"
    assert get(got, "properties", "a", "default", "properties") is None
    assert get(got, "$defs", "Person", "properties", "name") is not None


def test_issue6732_properties_named_default_and_type():
    # TestIssue6732_PropertiesNamedDefaultAndTypeRemainSchemas
    inp = J(r'''{"type":"object","properties":{"default":{"$ref":"#/$defs/Person"},
        "type":{"type":"string","title":"Kind"}},"$defs":{"Person":{"type":"object",
        "properties":{"name":{"type":"string"}}}}}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert get(got, "properties", "default", "properties", "name", "type") == "string", json.dumps(got)
    assert get(got, "properties", "type", "title") is None, json.dumps(got)


def test_issue6732_expansion_budget_bounded():
    # TestIssue6732_ExpansionBudgetBoundsSharedDefs
    inp = _diamond_input(12)
    got = gsc.clean_gemini_json_schema(inp)
    blob = json.dumps(got)
    assert len(blob) < 512 * 1024, len(blob)
    assert get(got, "properties", "leaf") is not None
    assert get(got, "properties", "leaf", "properties", "a", "properties", "a") is not None
    assert "$defs" not in got


def test_issue6732_deterministic_unused_defs():
    # TestIssue6732_UnusedDefinitionsDoNotConsumeExpansionBudget (5 runs stand in for Go's 30)
    defs = {"leaf0": {"type": "string", "description": "leaf leaf leaf leaf leaf leaf leaf leaf leaf leaf"},
            "Person": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}
    for i in range(13):
        target = "leaf0" if i == 0 else "D%d" % (i - 1)
        defs["D%d" % i] = {"type": "object", "properties": {
            "a": {"$ref": "#/$defs/" + target}, "b": {"$ref": "#/$defs/" + target}}}
    inp = {"type": "object", "properties": {"person": {"$ref": "#/$defs/Person"}},
           "required": ["person"], "$defs": defs}
    first = gsc.clean_gemini_json_schema(inp)
    for _ in range(5):
        assert gsc.clean_gemini_json_schema(inp) == first
    assert get(first, "properties", "person", "properties", "name", "type") == "string"
    assert "$defs" not in first


def test_issue6732_budget_fallback_resolves_siblings():
    # TestIssue6732_BudgetFallbackStillResolvesSiblings
    inp = {"type": "object", "properties": {"a": {"$ref": "#/$defs/Big",
                                                  "properties": {"x": {"$ref": "#/$defs/S"}}}},
           "$defs": {"Big": {"type": "object", "description": "x" * (280 * 1024)},
                     "S": {"type": "string"}}}
    got = gsc.clean_gemini_json_schema(inp)
    assert get(got, "properties", "a", "properties", "x", "type") == "string", json.dumps(got)[:200]
    assert '"$ref"' not in json.dumps(got)


def test_issue6732_large_description_fanout_bounded():
    # TestIssue6732_BudgetFallbackDoesNotCopyLargeDescriptions
    props = {"p%d" % i: {"$ref": "#/$defs/Big"} for i in range(200)}
    inp = {"type": "object", "properties": props,
           "$defs": {"Big": {"type": "object", "description": "x" * (32 * 1024),
                             "properties": {"name": {"type": "string"}}}}}
    got = gsc.clean_gemini_json_schema(inp)
    blob = json.dumps(got)
    assert len(blob) < 512 * 1024, len(blob)
    assert get(got, "properties", "p0") is not None and get(got, "properties", "p199") is not None
    assert get(got, "properties", "p0", "properties", "name", "type") == "string"
    assert "$defs" not in got


def test_issue6732_escaped_description_bounded():
    # TestIssue6732_EscapedDescriptionFanOutStaysBounded
    inp = {"type": "object",
           "properties": {"p%d" % i: {"$ref": "#/$defs/Big"} for i in range(8)},
           "$defs": {"Big": {"type": "object", "description": "<" * (32 * 1024),
                             "properties": {"name": {"type": "string"}}}}}
    got = gsc.clean_gemini_json_schema(inp)
    assert len(json.dumps(got)) < 512 * 1024


def test_issue6732_small_fanout_bounded():
    # TestIssue6732_SmallTargetFanOutStaysBounded
    inp = {"type": "object",
           "properties": {"p%d" % i: {"$ref": "#/$defs/S"} for i in range(2000)},
           "$defs": {"S": {"type": "string", "description": "x" * 512}}}
    got = gsc.clean_gemini_json_schema(inp)
    assert len(json.dumps(got)) < 512 * 1024


def test_issue6732_nested_unused_defs_graph():
    # TestIssue6732_NestedUnusedDefinitionsGraphDoesNotConsumeBudget (64 refs; semantics unchanged)
    inner_defs = {"Leaf": {"type": "string", "description": "x" * 1024}}
    for i in range(64):
        inner_defs["U%d" % i] = {"$ref": "#/properties/a/$defs/Leaf"}
    inp = {"type": "object",
           "properties": {"person": {"$ref": "#/$defs/Person"},
                          "a": {"type": "object", "$defs": inner_defs}},
           "required": ["person"],
           "$defs": {"Person": {"type": "object", "properties": {"name": {"type": "string"}},
                                "required": ["name"]}}}
    got = gsc.clean_gemini_json_schema(inp)
    assert get(got, "properties", "person", "properties", "name", "type") == "string"
    assert get(got, "properties", "a", "$defs") is None and "$defs" not in got


def test_issue6732_ref_sibling_name_map_refs():
    # TestIssue6732_RefSiblingNameMapReferencesStillExpand
    inp = J(r'''{"type":"object","properties":{"a":{"$ref":"#/$defs/Base",
        "properties":{"x":{"$ref":"#/$defs/S"}}}},"$defs":{"Base":{"type":"object"},"S":{"type":"string"}}}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert get(got, "properties", "a", "properties", "x", "type") == "string", json.dumps(got)


def test_issue6732_clean_inlining_strips_defs_from_intermediate():
    # TestIssue6732_CleanInliningStripsDefinitionsFromIntermediate
    person = {"type": "object", "properties": {"name": {"type": "string"}},
              "$defs": {"Unused": {"type": "string", "description": "x" * (280 * 1024)}}}
    inp = {"type": "object", "properties": {"p0": {"$ref": "#/$defs/Person"},
                                            "p1": {"$ref": "#/$defs/Person"}},
           "$defs": {"Person": person}}
    got = gsc.clean_json_schema(inp, gsc._GEMINI_JSON_SCHEMA_OPTIONS)
    blob = json.dumps(got)
    assert len(blob) < 64 * 1024, len(blob)
    assert "$defs" not in got and get(got, "properties", "p0", "$defs") is None
    assert get(got, "properties", "p1", "properties", "name", "type") == "string"


def test_issue6732_array_ref_keeps_target_items():
    # TestIssue6732_ArrayRefWithExplicitTypeKeepsTargetItems
    inp = J(r'''{"type":"object","properties":{"rows":{"type":"array","$ref":"#/$defs/Rows"}},
        "$defs":{"Rows":{"type":"array","items":{"type":"object","properties":{"name":{"type":"string"}}}}}}''')
    got = gsc.clean_gemini_json_schema(inp)
    assert get(got, "properties", "rows", "items", "properties", "name", "type") == "string", json.dumps(got)


def test_issue6732_array_ref_placeholder_idempotent():
    # TestIssue6732_ArrayRefWithoutTargetItemsGetsPlaceholder
    inp = J(r'''{"type":"object","properties":{"rows":{"type":"array","$ref":"#/$defs/Common"}},
        "$defs":{"Common":{"description":"Rows"}}}''')
    first = legacy_tool(inp)
    assert get(first, "properties", "rows", "items", "type") == "string", json.dumps(first)
    assert legacy_tool(first) == first


def test_issue6732_synthesized_type_no_override():
    # TestIssue6732_SynthesizedTypeDoesNotOverrideRefTarget
    inp = J(r'''{"$ref":"#/$defs/O","items":{"type":"integer"},"$defs":{"O":{"type":"object",
        "properties":{"x":{"type":"string"}}}}}''')
    first = gsc.clean_gemini_json_schema(inp)
    assert first["type"] == "object", json.dumps(first)
    assert "items" not in first, json.dumps(first)
    assert get(first, "properties", "x", "type") == "string"
    assert gsc.clean_gemini_json_schema(first) == first


def test_issue6732_property_named_ref_and_const():
    # TestIssue6732_PropertyNamedRefRemainsProperty / PropertyNamedConstRemainsProperty
    got = gsc.clean_gemini_json_schema(J(r'''{"type":"object","properties":{"$ref":{"type":"string"}}}'''))
    assert get(got, "properties", "$ref", "type") == "string", json.dumps(got)
    assert "See:" not in json.dumps(got)
    got2 = gsc.clean_gemini_json_schema(J(r'''{"type":"object","properties":{"const":{"type":"string"}}}'''))
    assert get(got2, "properties", "const", "type") == "string", json.dumps(got2)
    assert "enum" not in got2


def test_issue6732_definition_names_keep_schema_semantics():
    # TestIssue6732_InlineLocalRefsDefinitionNamesKeepSchemaSemantics
    inp = J(r'''{"$defs":{"S":{"type":"string"},"properties":{"type":"object","default":{"$ref":"#/$defs/S"}},
        "default":{"type":"object","properties":{"x":{"$ref":"#/$defs/S"}}}}}''')
    got = gsc.inline_local_refs(inp)
    assert get(got, "$defs", "properties", "default", "$ref") == "#/$defs/S"
    assert get(got, "$defs", "default", "properties", "x", "type") == "string"


def test_issue6732_dependencies_name_map():
    # TestIssue6732_DependenciesNameMapKeepsSchemaSemantics
    inp = J(r'''{"type":"object","dependencies":{"default":{"properties":{"x":{"$ref":"#/definitions/S"}}}},
        "definitions":{"S":{"type":"string"}}}''')
    got = gsc.inline_local_refs(inp)
    assert get(got, "dependencies", "default", "properties", "x", "type") == "string"
    assert get(got, "dependencies", "default", "properties", "x", "$ref") is None


def test_issue6732_omitted_fallback_description_not_charged():
    # TestIssue6732_OmittedFallbackDescriptionNotCharged
    inp = {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}, "b": {"$ref": "#/$defs/B"}},
           "$defs": {"A": {"type": "string", "description": "x" * 262142},
                     "B": {"type": "object", "properties": {"name": {"type": "string"}}}}}
    got = gsc.clean_gemini_json_schema(inp)
    assert get(got, "properties", "b", "properties", "name", "type") == "string", json.dumps(got)[:200]


# --- API-shape and safety contracts ---------------------------------------------------

def test_options_fields_and_presets():
    # jsonSchemaCleanOptions field set/order (gemini_schema.go:30-47)
    names = [f.name for f in dataclasses.fields(gsc.JsonSchemaCleanOptions)]
    assert names == ["add_placeholder", "add_missing_array_items", "antigravity_semantics",
                     "remove_tool_title", "remove_gemini_metadata", "flatten_unions",
                     "flatten_multi_branch_unions", "force_enum_string_type", "drop_all_enums",
                     "drop_boolean_enums", "preserve_additional_properties_false",
                     "preserve_all_additional_properties", "preserve_standard_constraints"], names
    # CleanJSONSchemaForAntigravityTool option literals (requirePlaceholder=true / false)
    assert gsc._antigravity_tool_options(True, False) == gsc.JsonSchemaCleanOptions(
        add_placeholder=True, add_missing_array_items=True, antigravity_semantics=True,
        remove_tool_title=False, flatten_unions=True, flatten_multi_branch_unions=False,
        drop_all_enums=True)
    assert gsc._antigravity_tool_options(False, True) == gsc.JsonSchemaCleanOptions(
        add_placeholder=False, add_missing_array_items=True, antigravity_semantics=True,
        remove_tool_title=True, flatten_unions=True, flatten_multi_branch_unions=True,
        drop_all_enums=True)
    assert gsc._ANTIGRAVITY_RESPONSE_OPTIONS == gsc.JsonSchemaCleanOptions(
        antigravity_semantics=True, flatten_unions=True, drop_boolean_enums=True,
        preserve_additional_properties_false=True)
    assert gsc._GEMINI_OPTIONS == gsc.JsonSchemaCleanOptions(
        add_missing_array_items=True, remove_gemini_metadata=True, flatten_unions=True,
        force_enum_string_type=True)
    assert gsc._GEMINI_JSON_SCHEMA_OPTIONS == gsc.JsonSchemaCleanOptions(
        add_missing_array_items=True, remove_gemini_metadata=True, flatten_unions=True,
        force_enum_string_type=True, preserve_all_additional_properties=True,
        preserve_standard_constraints=True)


def test_no_mutation_of_caller():
    original = {"type": "object", "properties": {"data": {"parent": {"type": "string", "required": True}}}}
    snapshot = json.dumps(original, sort_keys=True)
    got = legacy_tool(original)
    assert json.dumps(original, sort_keys=True) == snapshot
    assert json.dumps(got, sort_keys=True) != snapshot


def test_api_request_document_guard():
    # isAPIRequestDocument: the cleaners are for single schemas, never whole requests.
    # normalizeMalformedSchemaObjects must not wrap a request document's bare maps.
    req = {"tools": [{"name": "f", "parent": {"type": "string", "required": True}}],
           "contents": []}
    got = gsc.clean_json_schema(req, gsc._GEMINI_OPTIONS)
    assert get(got, "tools", "0", "properties") is None
    assert get(got, "tools", "0", "parent", "type") == "string"


# --- real-world corpus from the integration (upstream 400 evidence) --------------------

def test_corpus_type_string_null():
    inp = J(r'''{"type":"object","properties":{"note":{"type":["string","null"]}}}''')
    for clean in (gsc.clean_antigravity_tool, gsc.clean_antigravity_claude_tool,
                  gsc.clean_antigravity_response):
        got = clean(inp)
        node = get(got, "properties", "note")
        assert node["type"] == "string" and node["nullable"] is True, json.dumps(got)
        assert_no_type_list(got, clean)


def test_corpus_string_integer_multi_branch():
    inp = J(r'''{"type":"object","properties":{"v":{"type":["string","integer"]}}}''')
    for clean in (gsc.clean_antigravity_tool, gsc.clean_antigravity_claude_tool,
                  gsc.clean_antigravity_response):
        got = clean(inp)
        assert get(got, "properties", "v", "type") == "string"
        assert "Accepts: string | integer" in get(got, "properties", "v", "description")
        assert_no_type_list(got, clean)


def test_corpus_three_branch_anyof():
    inp = J(r'''{"type":"object","properties":{"payload":{"anyOf":[
        {"type":"string"},{"type":"integer"},
        {"type":"object","properties":{"kind":{"type":"string"}},"required":["kind"]}]}}}''')
    gem = gsc.clean_antigravity_tool(inp)
    assert len(get(gem, "properties", "payload", "anyOf")) == 3
    cla = gsc.clean_antigravity_claude_tool(inp)
    p = get(cla, "properties", "payload")
    assert "anyOf" not in p and p["type"] == "object"
    assert get(p, "properties", "kind") is not None and "Accepts:" in p["description"]
    assert_no_type_list(gem, "gemini")
    assert_no_type_list(cla, "claude")


def test_corpus_ref_with_sibling_required():
    inp = J(r'''{"type":"object","properties":{"node":{"$ref":"#/$defs/node","required":["id"]}},
        "$defs":{"node":{"type":"object","properties":{"id":{"type":"string"},
        "name":{"type":"string"}},"required":["id"]}}}''')
    got = gsc.clean_antigravity_claude_tool(inp)
    node = get(got, "properties", "node")
    assert "$ref" not in json.dumps(got) and "$defs" not in got
    assert node["type"] == "object" and node["required"] == ["id"]
    assert get(node, "properties", "id", "type") == "string"
    assert_no_type_list(got, "ref-sibling-required")

    conflict = {"type": "object",
                "properties": {"node": {"$ref": "#/$defs/node", "required": ["name"]}},
                "$defs": {"node": {"type": "object", "properties": {"id": {"type": "string"}},
                                   "required": ["id"]}}}
    got2 = gsc.clean_antigravity_response(conflict)
    node2 = get(got2, "properties", "node")
    # CPA merges sibling assertions into the resolved target when keywords do not conflict
    # (mergeInlinedRefSchemas: equal 'required' is not a collision), then the required
    # branch referencing an unknown key is pruned by cleanupRequiredFields.
    assert "allOf" not in node2 or node2.get("allOf") is None
    assert node2["type"] == "object" and get(node2, "properties", "id", "type") == "string"
    assert "$defs" not in got2 and "$ref" not in json.dumps(got2)


def test_corpus_array_missing_items():
    inp = J(r'''{"type":"object","properties":{"rows":{"type":"array"}}}''')
    assert get(gsc.clean_antigravity_tool(inp), "properties", "rows", "items") == {"type": "string"}
    assert get(gsc.clean_antigravity_claude_tool(inp), "properties", "rows", "items") == {"type": "string"}


def test_corpus_enum_title_default_triple():
    base = {"type": "object", "properties": {"mode": {
        "type": "string", "title": "Mode", "enum": ["a", "b", "c"], "default": "a"}}}
    cla = gsc.clean_antigravity_claude_tool(base)
    m = get(cla, "properties", "mode")
    assert "enum" not in m and "default" not in m and m["title"] == "Mode"
    assert "Allowed: a, b, c" in m["description"] and "default: a" in m["description"]
    gem = gsc.clean_antigravity_tool(base)
    assert "title" not in get(gem, "properties", "mode")
    assert "Allowed: a, b, c" in get(gem, "properties", "mode", "description")
    resp = gsc.clean_antigravity_response(base)
    r = get(resp, "properties", "mode")
    assert r["enum"] == ["a", "b", "c"] and r["type"] == "string" and r["title"] == "Mode"
    assert "default" not in r and "default: a" in r["description"]
    for got in (cla, gem, resp):
        assert_no_type_list(got, "corpus-enum-triple")


def test_corpus_all_cleaners_have_no_type_lists():
    corpus = [
        {"type": "object", "properties": {"a": {"type": ["string", "null"]},
                                          "b": {"type": ["string", "integer"]},
                                          "c": {"type": ["array", "null"]},
                                          "d": {"anyOf": [{"type": "string"}, {"type": ["integer", "null"]}]}},
         "required": ["a"]},
        {"type": ["object", "null"], "properties": {"x": {"type": "string"}}},
    ]
    for name, clean in NO_PLACEHOLDER_CLEANERS.items():
        for doc in corpus:
            got = clean(doc)
            assert_no_type_list(got, name)


def test_integration_presets_are_the_preset_functions():
    # Presets must equal explicit-flag calls (integration contract).
    inp = {"type": "object", "properties": {"q": {"type": ["string", "null"]},
                                             "n": {"type": "integer", "enum": [1, 2]}}}
    assert gsc.clean_antigravity_tool(inp) == gsc.antigravity_tool_schema(
        inp, require_placeholder=False, flatten_multi_branch_unions=False)
    assert gsc.clean_antigravity_claude_tool(inp) == gsc.antigravity_tool_schema(
        inp, require_placeholder=True, flatten_multi_branch_unions=True)
    # The VALIDATED claude preset carries the reason placeholder rules.
    empty = gsc.clean_antigravity_claude_tool({"type": "object"})
    assert get(empty, "properties", "reason", "description") == \
        "Brief explanation of why you are calling this tool"


def main():
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    failures = []
    for name, fn in tests:
        try:
            fn()
            print("PASS %s" % name)
        except Exception as exc:  # noqa: BLE001
            import traceback
            failures.append(name)
            print("FAIL %s: %s" % (name, exc))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - len(failures), len(tests)))
    if failures:
        print("failed: %s" % ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
