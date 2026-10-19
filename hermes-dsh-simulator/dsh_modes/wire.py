# -*- coding: utf-8 -*-
"""Provider wire shapes for the two DSH tools — a 1:1 mirror of Hermes' own
serializers.

Each factory takes the embedded DSH schemas and produces exactly what the
host's transport layer produces from an OpenAI-form
``{"type": "function", "function": {...}}`` tool entry:

* ``chat_completions``  — identity (``agent/transports/chat_completions.py::convert_tools``)
* ``codex_responses``   — ``agent/codex_responses_adapter.py::_responses_tools``
* ``anthropic_messages``— ``agent/anthropic_message_convert.py::convert_tools_to_anthropic``
* ``bedrock_converse``  — ``agent/bedrock_adapter.py::convert_tools_to_converse``

Tests assert equality against the live converter functions whenever Hermes is
importable, so this file cannot silently drift from the host.

``anthropic_tools()`` additionally mirrors the host's input-schema normalization
(``tools.schema_sanitizer.strip_nullable_unions(keep_nullable_hint=False)``
plus the top-level combinator strip applied by
``anthropic_message_convert._normalize_tool_input_schema``) — the byte-equality
test covers that too.
"""

from __future__ import annotations

from . import constants as C


def _dsh_schemas():
    return (C.active_bash_schema(), C.editor_schema())


def chat_tools():
    """OpenAI chat-completions tool defs."""
    return [{"type": "function", "function": schema} for schema in _dsh_schemas()]


def responses_tools():
    """OpenAI Responses / Codex function tool defs."""
    return [
        {
            "type": "function",
            "name": schema["name"],
            "description": schema.get("description", ""),
            "strict": False,
            "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
        }
        for schema in _dsh_schemas()
    ]


# -- Anthropic input_schema normalization (mirror of the host's sanitizer) ----
# Host chain: tools.schema_sanitizer.strip_nullable_unions(..., keep_nullable_hint=False)
# -> drop top-level oneOf/allOf/anyOf -> ensure properties. Kept in lockstep by
# the byte-equality test against agent.anthropic_message_convert.

_UNION_KEYS = ("anyOf", "oneOf")
_UNION_META_KEYS = ("title", "description", "default", "examples")


def _rewrite(node, fn):
    if isinstance(node, list):
        return [_rewrite(item, fn) for item in node]
    if not isinstance(node, dict):
        return node
    return fn({key: _rewrite(value, fn) for key, value in node.items()})


def _collapse_nullable_unions(node):
    for key in _UNION_KEYS:
        variants = node.get(key)
        if not isinstance(variants, list):
            continue
        non_null = [item for item in variants
                    if not (isinstance(item, dict) and item.get("type") == "null")]
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            for meta_key in _UNION_META_KEYS:
                if meta_key in node and meta_key not in replacement \
                        and not (meta_key == "default" and "$ref" in replacement):
                    replacement[meta_key] = node[meta_key]
            return _rewrite(replacement, _collapse_nullable_unions)  # survivor may nest unions
    return node


def normalize_input_schema(schema):
    """Anthropic-validator-friendly copy of a parameters schema."""
    normalized = _rewrite(schema, _collapse_nullable_unions) if schema else None
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    banned = {"oneOf", "allOf", "anyOf"}
    if banned & normalized.keys():
        normalized = {k: v for k, v in normalized.items() if k not in banned}
        normalized.setdefault("type", "object")
    if normalized.get("type") == "object" and not isinstance(normalized.get("properties"), dict):
        normalized = {**normalized, "properties": {}}
    return normalized


def anthropic_tools():
    """Anthropic Messages tool defs (``input_schema`` form, normalized)."""
    return [
        {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "input_schema": normalize_input_schema(schema.get("parameters")),
        }
        for schema in _dsh_schemas()
    ]


def bedrock_tools():
    """Bedrock Converse tool defs (``toolSpec`` form)."""
    return [
        {
            "toolSpec": {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "inputSchema": {"json": schema.get("parameters", {"type": "object", "properties": {}})},
            }
        }
        for schema in _dsh_schemas()
    ]


def tool_wire_name(entry):
    """Tool name from any wire shape we may need to recognize (or strip).

    Handles OpenAI ``{function: {name}}``, Responses/Anthropic ``{name}`` and
    Bedrock ``{toolSpec: {name}}`` entries; returns ``None`` for nameless
    entries (e.g. Bedrock ``cachePoint`` markers).
    """
    if isinstance(entry, dict):
        function = entry.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        if isinstance(entry.get("name"), str):
            return entry["name"]
        spec = entry.get("toolSpec")
        if isinstance(spec, dict) and isinstance(spec.get("name"), str):
            return spec["name"]
        return None
    return getattr(entry, "name", None)