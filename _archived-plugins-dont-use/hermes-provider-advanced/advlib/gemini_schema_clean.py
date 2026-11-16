"""Faithful Python port of CLIProxyAPI's JSON-schema cleaning engine.

Ported from ``internal/util/gemini_schema.go`` (CPA, 2401 lines) plus the
``WalkSchema``/``walk`` helpers in ``internal/util/translator.go``. CPA works
on JSON strings with gjson/sjson path semantics; this module works on decoded
Python objects (dicts/lists) with a minimal path-walk layer that reproduces
gjson/sjson behaviour exactly, including the dot/star/question escape rules
for author-chosen keys (a literal key ``a.b`` is the path ``a\\.b``).

Pass a single JSON schema to the functions below -- never a whole request document.
Cleaning walks every node and rewrites keys by name, and schema keywords such as
"title", "format", "default" and "const" are also ordinary data keys. Handing
these functions a request silently rewrites tool-call arguments inside the
conversation history: the guard that protects a key under ".properties" does not
apply to argument values, so the keys are deleted outright and replacements such
as "enum" and "type" are fabricated. That regression reached production once
already; scope every call site to the schema itself.
(gemini_schema.go:21-28)

Public API:
- clean_json_schema(schema, options) -> Any
- antigravity_tool_schema(schema, *, require_placeholder, flatten_multi_branch_unions)
- clean_antigravity_tool / clean_antigravity_claude_tool / clean_antigravity_response
- clean_gemini / clean_gemini_json_schema / inline_local_refs (extra parity entry points)

Python ints are converted to exact decimal numbers (_Num, the json.Number
equivalent) on entry and demoted back to int/float on output, so large
integers (9007199254740993) and enum digits never suffer precision drift.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, List

__all__ = [
    "JsonSchemaCleanOptions",
    "clean_json_schema",
    "antigravity_tool_schema",
    "clean_antigravity_claude_tool_schema",
    "clean_antigravity_tool",
    "clean_antigravity_claude_tool",
    "clean_antigravity_response",
    "clean_gemini",
    "clean_gemini_json_schema",
    "inline_local_refs",
    "PLACEHOLDER_REASON_DESCRIPTION",
]

PLACEHOLDER_REASON_DESCRIPTION = "Brief explanation of why you are calling this tool"  # gemini_schema.go:19
placeholder_reason_description = PLACEHOLDER_REASON_DESCRIPTION  # compat alias

# --- gjson path syntax -------------------------------------------------------
# gjson/sjson treat '.', '*' and '?' as wildcards inside path segments; keys
# containing them must be backslash-escaped (gemini_schema.go:17, 2336-2386).

_HAS_SPECIAL = re.compile(r"[.*?]")
_UNESCAPE = re.compile(r"\\(.)", re.DOTALL)


def _escape_key(key: str) -> str:
    """escapeGJSONPathKey (gemini_schema.go:2336) -- '.' '*' '?' become escaped."""
    if _HAS_SPECIAL.search(key) is None:
        return key
    return key.replace(".", "\\.").replace("*", "\\*").replace("?", "\\?")


def _unescape_key(key: str) -> str:
    """unescapeGJSONPathKey (gemini_schema.go:2343) -- drop one backslash before any next char."""
    if "\\" not in key:
        return key
    return _UNESCAPE.sub(r"\1", key)


def _split_path(path: str) -> List[str]:
    """splitGJSONPath (gemini_schema.go:2360) -- split on unescaped dots; pairs kept."""
    if path == "":
        return []
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(path)
    while i < n:
        c = path[i]
        if c == "\\" and i + 1 < n:
            buf.append("\\")
            i += 1
            buf.append(path[i])
            i += 1
            continue
        if c == ".":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _join_path(base: str, suffix: str) -> str:
    """joinPath (gemini_schema.go:2214)."""
    return suffix if base == "" else base + "." + suffix


def _trim_suffix(path: str, suffix: str) -> str:
    """trimSuffix (gemini_schema.go:2207) -- a path equal to the leading-dot-stripped
    suffix maps to "" (root node); otherwise TrimSuffix."""
    if path == suffix.lstrip("."):
        return ""
    if suffix and path.endswith(suffix):
        return path[: -len(suffix)]
    return path


def _sort_by_depth(paths: List[str]) -> None:
    """sortByDepth (gemini_schema.go:2201) -- stable sort, deepest (most segments) first."""
    paths.sort(key=lambda p: -len(_split_path(p)))


_MISSING = object()


def _get_node(doc: Any, path: str):
    """gjson.Get root-relative; returns _MISSING only when truly absent (a present
    JSON null reads as None, matching gjson.Null != gjson.Invalid). Segments are
    dict keys or, for lists, integer indexes; a non-numeric segment on a list misses."""
    if path == "":
        return doc
    cur = doc
    for seg in _split_path(path):
        key = _unescape_key(seg)
        if isinstance(cur, dict):
            if key not in cur:
                return _MISSING
            cur = cur[key]
        elif isinstance(cur, list):
            if not re.fullmatch(r"\d+", key):
                return _MISSING
            i = int(key)
            if i >= len(cur):
                return _MISSING
            cur = cur[i]
        else:
            return _MISSING
    return cur


def _exists(doc: Any, path: str) -> bool:
    """gjson Result.Exists()."""
    return _get_node(doc, path) is not _MISSING


def _get_child(node: Any, key: str):
    """gjson Result.Get on a materialized value: string keys on dicts, numeric
    indexes on lists; anything else reads as absent. ``key`` is the raw (already
    unescaped) name -- gjson path escaping is irrelevant for a single lookup."""
    if isinstance(node, dict):
        return node.get(key, _MISSING)
    if isinstance(node, list) and re.fullmatch(r"\d+", key):
        i = int(key)
        if i < len(node):
            return node[i]
    return _MISSING


def _set_path(doc: Any, path: str, value: Any) -> None:
    """sjson.Set/SetRaw at a literal path (creating dicts on the way), root-relative.
    List elements may be replaced in place; sjson cannot build array holes for a
    missing index (strconv error equivalent), so those paths are a no-op."""
    parts = _split_path(path)
    cur = doc
    for seg in parts[:-1]:
        key = _unescape_key(seg)
        if isinstance(cur, dict):
            nxt = cur.get(key, _MISSING)
            if nxt is _MISSING or not isinstance(nxt, (dict, list)):
                nxt = {}
                cur[key] = nxt
            cur = nxt
        elif isinstance(cur, list):
            if not re.fullmatch(r"\d+", key):
                return
            i = int(key)
            if i >= len(cur):
                return
            nxt = cur[i]
            if not isinstance(nxt, (dict, list)):
                nxt = {}
                cur[i] = nxt
            cur = nxt
        else:
            return
    last = _unescape_key(parts[-1])
    if isinstance(cur, dict):
        cur[last] = value
    elif isinstance(cur, list):
        if re.fullmatch(r"\d+", last) and int(last) < len(cur):
            cur[int(last)] = value


def _delete_path(doc: Any, path: str) -> None:
    """sjson.Delete root-relative; absent paths are a no-op."""
    parts = _split_path(path)
    cur = doc
    for seg in parts[:-1]:
        key = _unescape_key(seg)
        if isinstance(cur, dict):
            cur = cur.get(key, _MISSING)
        elif isinstance(cur, list) and re.fullmatch(r"\d+", key) and int(key) < len(cur):
            cur = cur[int(key)]
        else:
            return
        if not isinstance(cur, (dict, list)):
            return
    last = _unescape_key(parts[-1])
    if isinstance(cur, dict):
        cur.pop(last, None)
    elif isinstance(cur, list) and re.fullmatch(r"\d+", last) and int(last) < len(cur):
        del cur[int(last)]


def _assign_at(root: Any, path: str, value: Any) -> Any:
    """setRawAt (gemini_schema.go:2221) -- path "" replaces the whole document."""
    if path == "":
        return value
    _set_path(root, path, value)
    return root


# --- number plumbing ----------------------------------------------------------

class _Num:
    """Exact decimal number, Go's json.Number equivalent. Comparisons behave
    numerically; hashing follows the canonical text."""
    __slots__ = ("s",)

    def __init__(self, s: str):
        self.s = s

    def f(self) -> float:
        return float(self.s)

    def __eq__(self, other):
        if isinstance(other, _Num):
            try:
                return self.s == other.s or self.f() == other.f()
            except ValueError:
                return self.s == other.s
        if isinstance(other, bool):
            return False
        if isinstance(other, (int, float)):
            try:
                return self.f() == other
            except ValueError:
                return False
        return NotImplemented

    def __hash__(self):
        return hash(self.s)

    def __repr__(self):
        return f"_Num({self.s!r})"


def _normalize_numbers(value: Any) -> Any:
    """json.Decoder.UseNumber() equivalent, applied on entry (idempotent): Python
    ints are arbitrary-precision so their text form is exact; floats already match
    Go float64 precision and are left untouched."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _Num(str(value))
    if isinstance(value, dict):
        return {k: _normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(v) for v in value]
    return value


_INT_RE = re.compile(r"[-+]?\d+")


def _demote_numbers(value: Any) -> Any:
    """Convert internal _Num decimals back to native JSON numbers on output (int
    for integral text, float otherwise), so results are plain json-serializable."""
    if isinstance(value, _Num):
        if _INT_RE.fullmatch(value.s):
            return int(value.s)
        return float(value.s)
    if isinstance(value, dict):
        return {k: _demote_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_demote_numbers(v) for v in value]
    return value


def _num_to_jsonable(value: Any) -> Any:
    if isinstance(value, _Num):
        if _INT_RE.fullmatch(value.s):
            return int(value.s)
        try:
            return float(value.s)
        except ValueError:
            return value.s
    return value


def _string_of(value: Any) -> str:
    """gjson Result.String(): strings verbatim, numbers as raw text, bools/null as
    JSON words, containers as raw JSON; invalid reads as ""."""
    if value is _MISSING:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, _Num):
        return value.s
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return _raw(value)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _raw(value: Any) -> str:
    """gjson Result.Raw equivalent (compact JSON)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=_num_to_jsonable)


def _bool_of(value: Any) -> bool:
    """gjson Result.Bool(): true for True, 'true'/'t' (case-insensitive 't'), non-zero numbers."""
    if value is True:
        return True
    if isinstance(value, str):
        return value == "true" or value.lower() == "t"
    if isinstance(value, _Num):
        try:
            return value.f() != 0
        except ValueError:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return False


def _truthy(value: Any) -> bool:
    """Go `map[key] != nil` on decoded JSON: absent AND JSON null are nil."""
    return value is not _MISSING and value is not None


# --- keyword tables -----------------------------------------------------------

_SCHEMA_NAME_MAP_KEYWORDS = {  # schemaNameMapKeywords (gemini_schema.go:2233)
    "properties", "patternProperties", "dependentSchemas",
    "dependencies", "$defs", "definitions",
}

_LITERAL_VALUE_KEYWORDS = {"default", "example", "examples", "const", "enum"}  # gemini_schema.go:1040


def _is_literal_value_keyword(key: str) -> bool:
    """isLiteralValueKeyword (gemini_schema.go:1048)."""
    return key in _LITERAL_VALUE_KEYWORDS


def is_property_definition(path: str) -> bool:
    """isPropertyDefinition (gemini_schema.go:2256) -- the trailing run of name-map
    keywords flips: properties=true, properties.properties=false, etc."""
    segments = _split_path(path)
    trailing = 0
    for i in range(len(segments) - 1, -1, -1):
        if _unescape_key(segments[i]) not in _SCHEMA_NAME_MAP_KEYWORDS:
            break
        trailing += 1
    return trailing % 2 == 1


def _is_known_schema_keyword_or_extension(key: str) -> bool:
    """isKnownSchemaKeywordOrExtension (gemini_schema.go:419)."""
    if key.startswith("x-"):
        return True
    return key in {
        "properties", "patternProperties", "additionalProperties", "items", "prefixItems",
        "$defs", "definitions", "dependentSchemas", "dependentRequired", "dependencies",
        "if", "then", "else", "not", "contains", "propertyNames",
        "unevaluatedProperties", "unevaluatedItems", "contentSchema", "additionalItems",
        "default", "const", "example", "examples", "discriminator", "xml", "externalDocs",
        "enumDescriptions", "enumTitles",
    }


def _is_non_object_declared_type(t: Any) -> bool:
    """isNonObjectDeclaredType (gemini_schema.go:435)."""
    if isinstance(t, str):
        return t != "" and t.lower() != "object"
    if isinstance(t, list):
        for item in t:
            if isinstance(item, str) and item.lower() == "object":
                return False
        return len(t) > 0
    return False


def _is_array_declared_type(t: Any) -> bool:
    """isArrayDeclaredType (gemini_schema.go:450)."""
    if isinstance(t, str):
        return t.lower() == "array"
    if isinstance(t, list):
        return any(isinstance(i, str) and i.lower() == "array" for i in t)
    return False


def _is_api_request_document(m: dict) -> bool:
    """isAPIRequestDocument (gemini_schema.go:464)."""
    for key in ("tools", "contents", "messages", "functionDeclarations", "function_declarations"):
        if isinstance(m.get(key), list):
            return True
    req = m.get("request")
    if isinstance(req, dict):
        return _is_api_request_document(req)
    return False


# --- walk / findPaths ---------------------------------------------------------

def _walk_collect(value: Any, path: str, fields, out: dict) -> None:
    """translator.go:40 walk(schemaAware=true) + walkForFields (gemini_schema.go:2169).
    Arrays walk as forward-ordered numeric index segments (walk descends them like
    keys; only walkForExtensions reverses, per its Go source). A key spelled like a
    literal-value keyword outside a name map is client data: recorded, never descended."""
    if isinstance(value, dict):
        for key_str, val in value.items():
            child = _join_path(path, _escape_key(key_str))
            if key_str in fields:
                out.setdefault(key_str, []).append(child)
            if _is_literal_value_keyword(key_str) and not is_property_definition(path):
                continue
            _walk_collect(val, child, fields, out)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _walk_collect(item, _join_path(path, str(i)), fields, out)


def _find_paths(doc: Any, field: str) -> List[str]:
    """findPaths (gemini_schema.go:2153) via WalkSchema."""
    out: dict = {}
    _walk_collect(doc, "", {field}, out)
    return out.get(field, [])


def _find_paths_by_fields(doc: Any, fields) -> dict:
    """findPathsByFields (gemini_schema.go:2159)."""
    out: dict = {}
    _walk_collect(doc, "", set(fields), out)
    return out


# --- description hint plumbing -------------------------------------------------

def _description_path(parent_path: str) -> str:
    """descriptionPath (gemini_schema.go:2268)."""
    if parent_path == "" or parent_path == "@this":
        return "description"
    return parent_path + ".description"


def merge_hint(existing: str, hint: str) -> str:
    """mergeHint (gemini_schema.go:2278) -- idempotent hint append."""
    if existing == "":
        return hint
    if existing == hint or existing.startswith(hint + " (") or ("(%s)" % hint) in existing:
        return existing
    return "%s (%s)" % (existing, hint)


def _append_hint(doc: Any, parent_path: str, hint: str) -> None:
    """appendHint (gemini_schema.go:2292)."""
    desc_path = _description_path(parent_path)
    merged = merge_hint(_string_of(_get_node(doc, desc_path)), hint)
    _set_path(doc, desc_path, merged)


def _append_hint_raw(raw_node: dict, hint: str) -> None:
    """appendHintRaw (gemini_schema.go:2303) -- operates on the node itself."""
    raw_node["description"] = merge_hint(_string_of(raw_node.get("description", _MISSING)), hint)


def _merge_description_raw(schema_node: dict, parent_desc: str) -> None:
    """mergeDescriptionRaw (gemini_schema.go:2388)."""
    child_desc = _string_of(schema_node.get("description", _MISSING))
    if child_desc == "":
        schema_node["description"] = parent_desc
    elif child_desc == parent_desc:
        return
    else:
        schema_node["description"] = "%s (%s)" % (parent_desc, child_desc)


def _get_strings(doc: Any, path: str) -> List[str]:
    """getStrings (gemini_schema.go:2310) -- array items coerced with .String()."""
    arr = _get_node(doc, path)
    if isinstance(arr, list):
        return [_string_of(item) for item in arr]
    return []


# --- options -------------------------------------------------------------------

@dataclass(frozen=True)
class JsonSchemaCleanOptions:
    """jsonSchemaCleanOptions (gemini_schema.go:30-47) -- same 12 fields, same order."""
    add_placeholder: bool = False
    add_missing_array_items: bool = False
    antigravity_semantics: bool = False
    remove_tool_title: bool = False
    remove_gemini_metadata: bool = False
    flatten_unions: bool = False
    flatten_multi_branch_unions: bool = False
    force_enum_string_type: bool = False
    drop_all_enums: bool = False
    drop_boolean_enums: bool = False
    preserve_additional_properties_false: bool = False
    preserve_all_additional_properties: bool = False
    preserve_standard_constraints: bool = False


# --- presets (option literals copied from the Go entry points) -------------------

def _antigravity_tool_options(require_placeholder: bool,
                              flatten_multi_branch_unions: bool) -> JsonSchemaCleanOptions:
    """cleanJSONSchemaForAntigravityTool (gemini_schema.go:74-84)."""
    return JsonSchemaCleanOptions(
        add_placeholder=require_placeholder,
        add_missing_array_items=True,
        antigravity_semantics=True,
        remove_tool_title=not require_placeholder,
        flatten_unions=True,
        flatten_multi_branch_unions=flatten_multi_branch_unions,
        drop_all_enums=True,
    )


# CleanJSONSchemaForAntigravityResponse (gemini_schema.go:98-105)
_ANTIGRAVITY_RESPONSE_OPTIONS = JsonSchemaCleanOptions(
    antigravity_semantics=True,
    flatten_unions=True,
    drop_boolean_enums=True,
    preserve_additional_properties_false=True,
)

# CleanJSONSchemaForGemini (gemini_schema.go:109-116)
_GEMINI_OPTIONS = JsonSchemaCleanOptions(
    add_missing_array_items=True,
    remove_gemini_metadata=True,
    flatten_unions=True,
    force_enum_string_type=True,
)

# CleanJSONSchemaForGeminiJSONSchema (gemini_schema.go:122-131)
_GEMINI_JSON_SCHEMA_OPTIONS = JsonSchemaCleanOptions(
    add_missing_array_items=True,
    remove_gemini_metadata=True,
    flatten_unions=True,
    force_enum_string_type=True,
    preserve_all_additional_properties=True,
    preserve_standard_constraints=True,
)


# --- Phase 0: normalizeMalformedSchemaObjects -------------------------------------

def _repair_schema_node(node: dict):
    """repairSchemaNode (gemini_schema.go:488). Returns (clone, modified)."""
    modified = False
    clone = dict(node)

    # 1. If not declared as a primitive/array type, collect bare property definition maps
    if not _is_non_object_declared_type(clone.get("type")):
        bare_props = {}
        for k, v in clone.items():
            if isinstance(v, dict) and not _is_known_schema_keyword_or_extension(k):
                bare_props[k] = v
        if bare_props:
            repaired_props, promoted_reqs, _ = _repair_property_map(bare_props)
            for k in bare_props:
                clone.pop(k, None)
            existing_props = clone.get("properties")
            if isinstance(existing_props, dict):
                new_props = dict(existing_props)
                new_props.update(repaired_props)
                clone["properties"] = new_props
            else:
                clone["properties"] = repaired_props
                if "type" not in clone:
                    clone["type"] = "object"
            if promoted_reqs:
                existing_reqs = _extract_string_array(clone.get("required"))
                merged = _merge_string_slices(existing_reqs, promoted_reqs)
                clone["required"] = merged  # Go assigns unconditionally when promoted non-empty
            modified = True

    # 2. Repair declared properties without inferring a type before refs are expanded.
    # Final sanitization handles native object and array schemas once refs/conjunctions resolve.
    props_val = clone.get("properties")
    if isinstance(props_val, dict):
        repaired_props, promoted_reqs, props_mod = _repair_property_map(props_val)
        if props_mod:
            clone["properties"] = repaired_props
            modified = True
        if promoted_reqs:
            existing_reqs = _extract_string_array(clone.get("required"))
            merged = _merge_string_slices(existing_reqs, promoted_reqs)
            clone["required"] = merged
            modified = True

    # 3. Recurse into all other standard schema containers
    items_val = clone.get("items")
    if isinstance(items_val, dict):
        repaired_items, items_mod = _repair_schema_node(items_val)
        if items_mod:
            clone["items"] = repaired_items
            modified = True
    elif isinstance(items_val, list):
        repaired_list, list_mod = _repair_schema_list(items_val)
        if list_mod:
            clone["items"] = repaired_list
            modified = True
    elif items_val is True:
        clone["items"] = {}
        modified = True

    add_props = clone.get("additionalProperties")
    if isinstance(add_props, dict):
        repaired, mod = _repair_schema_node(add_props)
        if mod:
            clone["additionalProperties"] = repaired
            modified = True

    pat_props = clone.get("patternProperties")
    if isinstance(pat_props, dict):
        repaired_pat, _, pat_mod = _repair_property_map(pat_props)
        if pat_mod:
            clone["patternProperties"] = repaired_pat
            modified = True

    for key in ("if", "then", "else", "not", "contains", "propertyNames",
                "unevaluatedProperties", "unevaluatedItems", "contentSchema", "additionalItems"):
        sub = clone.get(key)
        if isinstance(sub, dict):
            repaired_sub, sub_mod = _repair_schema_node(sub)
            if sub_mod:
                clone[key] = repaired_sub
                modified = True
        elif sub is True:
            clone[key] = {}
            modified = True

    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        list_val = clone.get(key)
        if isinstance(list_val, list):
            repaired_list, list_mod = _repair_schema_list(list_val)
            if list_mod:
                clone[key] = repaired_list
                modified = True

    for key in ("$defs", "definitions", "dependentSchemas", "dependencies"):
        defs_val = clone.get(key)
        if isinstance(defs_val, dict):
            repaired_defs = {}
            defs_modified = False
            for dk, dv in defs_val.items():
                if isinstance(dv, dict):
                    repaired_def, def_mod = _repair_schema_node(dv)
                    repaired_defs[dk] = repaired_def
                    if def_mod:
                        defs_modified = True
                        modified = True
                elif dv is True:
                    repaired_defs[dk] = {}
                    defs_modified = True
                    modified = True
                else:
                    repaired_defs[dk] = dv
            if defs_modified:
                clone[key] = repaired_defs

    return clone, modified


def _repair_schema_list(lst: list):
    """repairSchemaList (gemini_schema.go:646)."""
    out = []
    modified = False
    for item in lst:
        if isinstance(item, dict):
            repaired, mod = _repair_schema_node(item)
            out.append(repaired)
            if mod:
                modified = True
        elif item is True:
            out.append({})
            modified = True
        else:
            out.append(item)
    return out, modified


def _repair_property_map(props: dict):
    """repairPropertyMap (gemini_schema.go:666). Returns (out, promotedReqs, modified)."""
    out = {}
    promoted = []
    modified = False
    for k, v in props.items():
        if v is True:
            out[k] = {}
            modified = True
            continue
        if not isinstance(v, dict):
            out[k] = v
            continue
        child = dict(v)
        if isinstance(child.get("required"), bool):
            req_bool = child.pop("required")
            modified = True
            if req_bool:
                promoted.append(k)
        repaired_child, child_mod = _repair_schema_node(child)
        if child_mod:
            modified = True
        out[k] = repaired_child
    promoted.sort()  # sort.Strings in Go
    return out, promoted, modified


def _extract_string_array(val: Any) -> List[str]:
    """extractStringArray (gemini_schema.go:708)."""
    if isinstance(val, list):
        return [x for x in val if isinstance(x, str)]
    return []


def _merge_string_slices(existing: List[str], promoted: List[str]) -> List[str]:
    """mergeStringSlices (gemini_schema.go:728) -- dedupe, skip empties."""
    seen = set()
    res = []
    for s in list(existing) + list(promoted):
        if s and s not in seen:
            seen.add(s)
            res.append(s)
    return res


def _normalize_malformed_schema_objects(root: Any):
    """normalizeMalformedSchemaObjects (gemini_schema.go:351) -- object-tree version.
    The API-request-document guard refuses to touch whole request payloads."""
    if root is True:
        return {}
    if not isinstance(root, dict) or _is_api_request_document(root):
        return root
    if len(root) == 1 and "schema" in root:
        inner = root["schema"]
        if isinstance(inner, dict):
            repaired, modified = _repair_schema_node(inner)
            if not modified:
                return root
            return {"schema": repaired}
        if inner is True:
            return {"schema": {}}
        # Go falls through to the general repair of the root map itself
    repaired, modified = _repair_schema_node(root)
    if not modified:
        return root
    return repaired


# --- ref inlining machinery -------------------------------------------------------

MAX_INLINED_SCHEMA_BYTES = 256 * 1024   # maxInlinedSchemaBytes (gemini_schema.go:756)
MAX_FALLBACK_DESCRIPTION_BYTES = 512    # maxFallbackDescriptionBytes (gemini_schema.go:760)


def _sorted_keys(m: dict) -> List[str]:
    """sortedKeys (gemini_schema.go:842) -- deterministic map order."""
    return sorted(m.keys())


def _json_value_size(value: Any) -> int:
    """jsonValueSize (gemini_schema.go:982) -- encoding/json default-escape estimate."""
    if value is None:
        return 4
    if isinstance(value, bool):
        return 5
    if isinstance(value, _Num):
        return len(value.s)
    if isinstance(value, str):
        return _json_string_encoded_size(value)
    if isinstance(value, list):
        size = 2
        for item in value:
            size += _json_value_size(item) + 1
        return size
    if isinstance(value, dict):
        size = 2
        for key, item in value.items():
            size += _json_string_encoded_size(key) + 1 + _json_value_size(item) + 1
        return size
    return 16


def _clean_expansion_size(value: Any, in_name_map: bool) -> int:
    """cleanExpansionSize (gemini_schema.go:928) -- definitions excluded when cleaning."""
    if value is None:
        return 4
    if isinstance(value, bool):
        return 5
    if isinstance(value, _Num):
        return len(value.s)
    if isinstance(value, str):
        return _json_string_encoded_size(value)
    if isinstance(value, list):
        size = 2
        for item in value:
            size += _clean_expansion_size(item, False) + 1
            if size > MAX_INLINED_SCHEMA_BYTES + 1:
                return MAX_INLINED_SCHEMA_BYTES + 1
        return size
    if isinstance(value, dict):
        size = 2
        for key, item in value.items():
            if not in_name_map and key in ("$defs", "definitions"):
                continue
            child_in_name_map = False
            if not in_name_map:
                if _is_literal_value_keyword(key):
                    size += _json_string_encoded_size(key) + 1 + _json_value_size(item) + 1
                    if size > MAX_INLINED_SCHEMA_BYTES + 1:
                        return MAX_INLINED_SCHEMA_BYTES + 1
                    continue
                if key in _SCHEMA_NAME_MAP_KEYWORDS:
                    child_in_name_map = True
            size += _json_string_encoded_size(key) + 1 + _clean_expansion_size(item, child_in_name_map) + 1
            if size > MAX_INLINED_SCHEMA_BYTES + 1:
                return MAX_INLINED_SCHEMA_BYTES + 1
        return size
    return 16


def _json_string_encoded_size(s: str) -> int:
    """jsonStringEncodedSize (gemini_schema.go:1011) -- Go utf8 byte scan with default
    HTML escaping: 6 bytes for < > &, 2 for \\n \\r \\t, 6 for other <0x20, 6 for U+2028/2029."""
    b = s.encode("utf-8", "surrogatepass")
    size = 2
    i = 0
    n = len(b)
    while i < n:
        c = b[i]
        if c < 0x20:
            size += 2 if c in (0x0A, 0x0D, 0x09) else 6
        elif c in (0x22, 0x5C):
            size += 2
        elif c in (0x3C, 0x3E, 0x26):
            size += 6
        elif c < 0x80:
            size += 1
        elif c == 0xE2 and i + 2 < n and b[i + 1] == 0x80 and b[i + 2] in (0xA8, 0xA9):
            size += 6
            i += 2
        else:
            size += 1
        i += 1
    return size


def _target_expansion_size(value: Any, for_clean: bool) -> int:
    """targetExpansionSize (gemini_schema.go:921)."""
    if not for_clean:
        return _json_value_size(value)
    return _clean_expansion_size(value, False)


def _equal_json(a: Any, b: Any) -> bool:
    """reflect.DeepEqual approximation for decoded JSON trees."""
    if isinstance(a, _Num) or isinstance(b, _Num):
        if isinstance(a, _Num) and isinstance(b, _Num):
            return a == b
        if isinstance(a, _Num) and not isinstance(b, bool) and isinstance(b, (int, float)):
            return a == b
        if isinstance(b, _Num) and not isinstance(a, bool) and isinstance(a, (int, float)):
            return a == b
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b if isinstance(a, bool) and isinstance(b, bool) else False
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) != len(b):
            return False
        for k, v in a.items():
            if k not in b or not _equal_json(v, b[k]):
                return False
        return True
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal_json(x, y) for x, y in zip(a, b))
    if a is None and b is None:
        return True
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, float) and isinstance(b, float):
        return a == b
    return False


def _deep_copy_json_value(value: Any) -> Any:
    """deepCopyJSONValue (gemini_schema.go:1225)."""
    if isinstance(value, list):
        return [_deep_copy_json_value(x) for x in value]
    if isinstance(value, dict):
        return {k: _deep_copy_json_value(v) for k, v in value.items()}
    return value


def _resolve_json_pointer(root: Any, ref: str):
    """resolveJSONPointer (gemini_schema.go:1244) -- ~1 -> '/', ~0 -> '~'."""
    current = root
    for raw_part in ref[len("#/"):].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                return None, False
            current = current[part]
        elif isinstance(current, list):
            if not re.fullmatch(r"[+-]?\d+", part):
                return None, False
            idx = int(part)
            if idx < 0 or idx >= len(current):
                return None, False
            current = current[idx]
        else:
            return None, False
    return current, True


def _ref_name(ref: str) -> str:
    """refName (gemini_schema.go:1297)."""
    i = ref.rfind("/")
    if i >= 0 and i + 1 < len(ref):
        return ref[i + 1:].replace("~1", "/").replace("~0", "~")
    return ref


def _cyclic_ref_fallback(node: dict, target: Any, ref: str) -> dict:
    """cyclicRefFallback (gemini_schema.go:1268) -- typed hint for cycles/over-budget refs."""
    out = {}
    if isinstance(target, dict):
        for key in ("type", "nullable", "description"):
            if key in target:
                if key == "description":
                    v = target[key]
                    if isinstance(v, str) and len(v.encode("utf-8")) > MAX_FALLBACK_DESCRIPTION_BYTES:
                        continue
                out[key] = target[key]
    for key, value in node.items():
        if key != "$ref":
            out[key] = value
    hint = "See: " + _ref_name(ref)
    existing = out.get("description")
    if isinstance(existing, str) and existing != "":
        out["description"] = merge_hint(existing, hint)
    else:
        out["description"] = hint
    return out


class _RefState:
    """refResolveState (gemini_schema.go:832) -- one pass's remaining expansion budget."""
    __slots__ = ("budget",)

    def __init__(self):
        self.budget = MAX_INLINED_SCHEMA_BYTES


def _merge_inlined_ref_schemas(target: dict, siblings: dict) -> dict:
    """mergeInlinedRefSchemas (gemini_schema.go:1114) -- keep both complete schemas when
    assertions collide; annotation siblings may still override the target's annotations."""
    if siblings and (_truthy(target.get("unevaluatedProperties"))
                     or _truthy(target.get("unevaluatedItems"))):
        return _conjoin_inlined_ref_schemas(target, siblings)
    # These assertions depend on adjacent keywords in their own schema; even equal
    # values must not inherit property declarations, tuple prefixes, or match counts.
    for a, b in (("additionalProperties", "properties"),
                 ("additionalProperties", "patternProperties"),
                 ("additionalProperties", "additionalProperties"),
                 ("items", "prefixItems"),
                 ("contains", "contains"),
                 ("contains", "minContains"),
                 ("contains", "maxContains")):
        if (_truthy(target.get(a)) and _truthy(siblings.get(b))) \
                or (_truthy(siblings.get(a)) and _truthy(target.get(b))):
            return _conjoin_inlined_ref_schemas(target, siblings)
    out = dict(target)
    for key, value in siblings.items():
        if key in ("title", "description", "default", "example", "examples",
                   "$comment", "deprecated", "readOnly", "writeOnly"):
            pass  # annotations do not relax validation constraints
        else:
            if key in target and not _equal_json(target[key], value):
                return _conjoin_inlined_ref_schemas(target, siblings)
        out[key] = value
    return out


def _conjoin_inlined_ref_schemas(target: dict, siblings: dict) -> dict:
    """conjoinInlinedRefSchemas (gemini_schema.go:1154) -- $ref -> allOf applicator;
    siblings are never relocated out of their evaluation scope."""
    out = dict(siblings)
    branches = [target]
    existing = siblings.get("allOf")
    if isinstance(existing, list):
        branches = branches + list(existing)
    out["allOf"] = branches
    return out


def _resolve_ref_fallback(root: Any, node: dict, target: Any, ref: str,
                          active: set, state: _RefState, for_clean: bool) -> dict:
    """resolveRefFallback (gemini_schema.go:1172) -- degraded refs still resolve siblings."""
    out = _cyclic_ref_fallback(node, target, ref)
    # The target description copied into the fallback is expansion output; charge it.
    # Omitted content (over the copy limit) is not charged.
    if isinstance(target, dict):
        description = target.get("description")
        if isinstance(description, str) and description != "" \
                and len(description.encode("utf-8")) <= MAX_FALLBACK_DESCRIPTION_BYTES:
            if "description" not in node:
                cost = _json_string_encoded_size(description)
                if cost > state.budget:
                    out["description"] = "See: " + _ref_name(ref)
                else:
                    state.budget -= cost
    for key in _sorted_keys(node):
        if key in ("$ref", "description"):
            continue
        if for_clean and key in ("$defs", "definitions"):
            out.pop(key, None)
            continue
        out[key] = _resolve_schema_entry(root, key, node[key], active, state, for_clean)
    return out


def _resolve_schema_entry(root: Any, key: str, item: Any, active: set,
                          state: _RefState, for_clean: bool) -> Any:
    """resolveSchemaEntry (gemini_schema.go:1209)."""
    if _is_literal_value_keyword(key):
        return _deep_copy_json_value(item)
    if key in _SCHEMA_NAME_MAP_KEYWORDS and isinstance(item, dict):
        out = {}
        for name in _sorted_keys(item):
            out[name] = _resolve_local_refs(root, item[name], active, state, False, for_clean)
        return out
    return _resolve_local_refs(root, item, active, state, False, for_clean)


def _resolve_local_refs(root: Any, value: Any, active: set, state: _RefState,
                        in_name_map: bool, for_clean: bool) -> Any:
    """resolveLocalRefs (gemini_schema.go:1053) -- budgeted expansion, conjunctive siblings."""
    if isinstance(value, list):
        return [_resolve_local_refs(root, item, active, state, False, for_clean) for item in value]
    if isinstance(value, dict):
        node = value
        if not in_name_map:
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                target, ok = _resolve_json_pointer(root, ref)
                if ok:
                    if ref in active:
                        return _resolve_ref_fallback(root, node, target, ref, active, state, for_clean)
                    size = _target_expansion_size(target, for_clean)
                    if size > state.budget:
                        return _resolve_ref_fallback(root, node, target, ref, active, state, for_clean)
                    state.budget -= size
                    active.add(ref)
                    resolved_target = _resolve_local_refs(root, target, active, state, False, for_clean)
                    active.discard(ref)
                    if isinstance(resolved_target, dict):
                        siblings = {}
                        for key in _sorted_keys(node):
                            if key == "$ref":
                                continue
                            if for_clean and key in ("$defs", "definitions"):
                                continue
                            siblings[key] = _resolve_schema_entry(root, key, node[key], active, state, for_clean)
                        return _merge_inlined_ref_schemas(resolved_target, siblings)
        out = {}
        for key in _sorted_keys(node):
            if for_clean and not in_name_map and key in ("$defs", "definitions"):
                continue
            out[key] = _resolve_schema_entry(root, key, node[key], active, state, for_clean)
        return out
    return value


def _resolve_schema_root(root: Any, state: _RefState, for_clean: bool) -> Any:
    """resolveSchemaRoot (gemini_schema.go:858) -- schema body first; definition
    containers only survive for the public inliner (with the leftover budget)."""
    if not isinstance(root, dict):
        return _resolve_local_refs(root, root, set(), state, False, for_clean)

    out = {}
    for key in _sorted_keys(root):
        item = root[key]
        if key in ("$defs", "definitions"):
            if not for_clean:
                out[key] = None  # placeholder, resolved after the schema body
        elif key == "$ref":
            out[key] = item
        else:
            out[key] = _resolve_schema_entry(root, key, item, set(), state, for_clean)

    ref = root.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target, ok_target = _resolve_json_pointer(root, ref)
        if ok_target:
            size = _target_expansion_size(target, for_clean)
            if size <= state.budget:
                state.budget -= size
                active = {ref}
                resolved = _resolve_local_refs(root, target, active, state, False, for_clean)
                if isinstance(resolved, dict):
                    out.pop("$ref", None)
                    out.pop("$defs", None)
                    out.pop("definitions", None)
                    out.pop("$schema", None)
                    out = _merge_inlined_ref_schemas(resolved, out)
                    if "$schema" in root:
                        out["$schema"] = root["$schema"]
                else:
                    out = _resolve_ref_fallback(root, out, target, ref, set(), state, for_clean)
            else:
                out = _resolve_ref_fallback(root, out, target, ref, set(), state, for_clean)
            out.pop("$ref", None)

    if not for_clean:
        for key in ("$defs", "definitions"):
            if key in root:
                out[key] = _resolve_schema_entry(root, key, root[key], set(), state, False)
    return out


def _contains_ref_marker(value: Any) -> bool:
    """strings.Contains(jsonStr, `\"$ref\"`) equivalent -- a key literally spelled $ref."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "$ref":
                return True
            if _contains_ref_marker(v):
                return True
    elif isinstance(value, list):
        for item in value:
            if _contains_ref_marker(item):
                return True
    return False


def inline_local_refs(schema: Any) -> Any:
    """InlineLocalRefs (gemini_schema.go:749) -- public inliner; definitions kept,
    every reference receives its own copy within the expansion budget."""
    doc = _normalize_numbers(copy.deepcopy(schema))
    if not _contains_ref_marker(doc):
        return schema
    state = _RefState()
    return _demote_numbers(_resolve_schema_root(doc, state, False))


def _inline_local_refs_for_clean(schema: Any) -> Any:
    """inlineLocalRefsForClean (gemini_schema.go:773) -- drops $defs/definitions.
    The single-key {"schema": ...} carrier resolves against the inner schema."""
    if not _contains_ref_marker(schema):
        return schema
    wrapped = (isinstance(schema, dict) and len(schema) == 1
               and isinstance(schema.get("schema"), dict))
    if not wrapped:
        state = _RefState()
        return _resolve_schema_root(schema, state, True)
    state = _RefState()
    resolved = _resolve_schema_root(schema["schema"], state, True)
    return {"schema": resolved}


# --- pipeline stages --------------------------------------------------------------

def _has_schema_conjunction(doc: Any) -> bool:
    """hasSchemaConjunction (gemini_schema.go:2036)."""
    return any(isinstance(_get_node(doc, path), list) for path in _find_paths(doc, "allOf"))


def _convert_refs_to_hints(doc: Any, preserve_siblings: bool) -> Any:
    """convertRefsToHints (gemini_schema.go:1306) -- unresolved/external refs only."""
    paths = _find_paths(doc, "$ref")
    _sort_by_depth(paths)
    for p in paths:
        parent_path = _trim_suffix(p, ".$ref")
        if is_property_definition(parent_path):
            continue  # a property literally named "$ref", not a reference keyword
        ref_str = _string_of(_get_node(doc, p))
        hint = "See: %s" % _ref_name(ref_str)
        if not preserve_siblings:
            existing = _string_of(_get_node(doc, _description_path(parent_path)))
            if existing != "":
                hint = "%s (%s)" % (existing, hint)
            doc = _assign_at(doc, parent_path, {"type": "object", "description": hint})
            continue
        _delete_path(doc, p)
        _append_hint(doc, parent_path, hint)
    return doc


def _convert_const_to_enum(doc: Any) -> Any:
    """convertConstToEnum (gemini_schema.go:1333)."""
    for p in _find_paths(doc, "const"):
        val = _get_node(doc, p)
        if val is _MISSING:
            continue
        parent_path = _trim_suffix(p, ".const")
        enum_path = _join_path(parent_path, "enum")
        if not _exists(doc, enum_path):
            _set_path(doc, enum_path, [copy.deepcopy(val)])
    return doc


def _convert_enum_values_to_strings(doc: Any, force_string_type: bool) -> Any:
    """convertEnumValuesToStrings (gemini_schema.go:1351) -- declared type stays independent."""
    for p in _find_paths(doc, "enum"):
        arr = _get_node(doc, p)
        if not isinstance(arr, list):
            continue
        _set_path(doc, p, [_string_of(item) for item in arr])
        if force_string_type:
            parent_path = _trim_suffix(p, ".enum")
            _set_path(doc, _join_path(parent_path, "type"), "string")
    return doc


def _add_enum_hints(doc: Any) -> Any:
    """addEnumHints (gemini_schema.go:1374) -- 2..10 values -> 'Allowed: a, b'."""
    for p in _find_paths(doc, "enum"):
        arr = _get_node(doc, p)
        if not isinstance(arr, list):
            continue
        if len(arr) <= 1 or len(arr) > 10:
            continue
        vals = [_string_of(item) for item in arr]
        _append_hint(doc, _trim_suffix(p, ".enum"), "Allowed: " + ", ".join(vals))
    return doc


def _drop_ignored_enums_to_hints(doc: Any, options: JsonSchemaCleanOptions) -> Any:
    """dropIgnoredEnumsToHints (gemini_schema.go:1397)."""
    for path in _find_paths(doc, "enum"):
        parent_path = _trim_suffix(path, ".enum")
        should_drop = options.drop_all_enums or (
            options.drop_boolean_enums
            and _string_of(_get_node(doc, _join_path(parent_path, "type"))) == "boolean")
        if not should_drop:
            continue
        enum = _get_node(doc, path)
        if isinstance(enum, list) and len(enum) == 1:
            _append_hint(doc, parent_path, "Allowed: " + _string_of(enum[0]))
        _delete_path(doc, path)
    return doc


def _add_additional_properties_hints(doc: Any) -> Any:
    """addAdditionalPropertiesHints (gemini_schema.go:1413)."""
    for p in _find_paths(doc, "additionalProperties"):
        if _get_node(doc, p) is False:
            _append_hint(doc, _trim_suffix(p, ".additionalProperties"), "No extra properties allowed")
    return doc


_UNSUPPORTED_CONSTRAINTS = [  # unsupportedConstraints (gemini_schema.go:1422)
    "minLength", "maxLength", "exclusiveMinimum", "exclusiveMaximum",
    "pattern", "minItems", "maxItems", "uniqueItems", "contains", "format",
    "default", "examples",  # Claude rejects these in VALIDATED mode
]


def _constraint_keywords(options: JsonSchemaCleanOptions) -> List[str]:
    """constraintKeywords (gemini_schema.go:1428)."""
    if options.preserve_standard_constraints:
        return []
    keywords = list(_UNSUPPORTED_CONSTRAINTS)
    if options.antigravity_semantics:
        keywords += ["minimum", "maximum", "multipleOf"]
    return keywords


def _move_constraints_to_description(doc: Any, options: JsonSchemaCleanOptions) -> Any:
    """moveConstraintsToDescription (gemini_schema.go:1439)."""
    constraints = _constraint_keywords(options)
    if not constraints:
        return doc
    by_field = _find_paths_by_fields(doc, constraints)
    for key in constraints:
        for p in by_field.get(key, []):
            val = _get_node(doc, p)
            if val is _MISSING:
                continue
            parent_path = _trim_suffix(p, "." + key)
            if is_property_definition(parent_path):
                continue
            if isinstance(val, (dict, list)):
                _append_hint(doc, parent_path, "%s: %s" % (key, _raw(val)))
            else:
                _append_hint(doc, parent_path, "%s: %s" % (key, _string_of(val)))
    return doc


def _move_not_to_description(doc: Any) -> Any:
    """moveNotToDescription (gemini_schema.go:1465)."""
    for path in _find_paths(doc, "not"):
        value = _get_node(doc, path)
        if value is _MISSING or is_property_definition(_trim_suffix(path, ".not")):
            continue
        _append_hint(doc, _trim_suffix(path, ".not"), "not: " + _raw(value))
    return doc


def _merge_conditionals(doc: Any) -> Any:
    """mergeConditionals (gemini_schema.go:1476) -- then/else branch properties are
    added into the parent's properties without overwriting existing definitions."""
    by_field = _find_paths_by_fields(doc, ["then", "else"])
    paths: List[str] = []
    for key in ("then", "else"):
        for p in by_field.get(key, []):
            if is_property_definition(_trim_suffix(p, "." + key)):
                continue
            paths.append(p)
    _sort_by_depth(paths)

    for p in paths:
        props = _get_node(doc, _join_path(p, "properties"))
        if not isinstance(props, dict):
            continue
        last = _split_path(p)[-1]
        if last == "then":
            parent_path = _trim_suffix(p, ".then")
        elif last == "else":
            parent_path = _trim_suffix(p, ".else")
        else:
            continue
        for prop_key, prop_val in props.items():
            dest = _join_path(parent_path, "properties." + _escape_key(prop_key))
            if not _exists(doc, dest):
                _set_path(doc, dest, copy.deepcopy(prop_val))
    return doc


def _merge_missing_schema_at_path(doc: Any, destination: str, incoming: Any) -> None:
    """mergeMissingSchemaAtPath (gemini_schema.go:1566) -- fills gaps, never overwrites."""
    existing = _get_node(doc, destination)
    if existing is _MISSING:
        _set_path(doc, destination, copy.deepcopy(incoming))
        return
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return
    for key, value in incoming.items():
        _merge_missing_schema_at_path(doc, _join_path(destination, _escape_key(key)), value)


def _merge_all_of(doc: Any) -> Any:
    """mergeAllOf (gemini_schema.go:1518) -- required arrays are unioned; if/then/else/allOf
    branches are dropped (unrepresentable upstream); everything else merges into gaps."""
    paths = _find_paths(doc, "allOf")
    _sort_by_depth(paths)
    for p in paths:
        all_of = _get_node(doc, p)
        if not isinstance(all_of, list):
            continue
        parent_path = _trim_suffix(p, ".allOf")
        for item in all_of:
            if not isinstance(item, dict):
                continue
            for field, value in item.items():
                if field == "required":
                    if not isinstance(value, list):
                        continue
                    req_path = _join_path(parent_path, "required")
                    current = _get_strings(doc, req_path)
                    for required in value:
                        name = _string_of(required)
                        if name not in current:
                            current.append(name)
                    _set_path(doc, req_path, current)
                elif field in ("if", "then", "else", "allOf"):
                    continue  # conditional applicability cannot be represented upstream
                else:
                    dest = _join_path(parent_path, _escape_key(field))
                    _merge_missing_schema_at_path(doc, dest, value)
        _delete_path(doc, p)
    return doc


def _is_required_only_union_branch(item: Any) -> bool:
    """isRequiredOnlyUnionBranch (gemini_schema.go:1583)."""
    if not isinstance(item, dict) or "required" not in item:
        return False
    for key in ("type", "properties", "items", "enum", "anyOf", "oneOf", "allOf"):
        if key in item:
            return False
    return True


def _materialize_required_only_branch(item: dict, parent_props: Any):
    """materializeRequiredOnlyBranch (gemini_schema.go:1595)."""
    if not isinstance(parent_props, dict):
        return None, False
    reqs = item.get("required")
    req_names = [_string_of(r) for r in reqs] if isinstance(reqs, list) else []
    if not req_names:
        return None, False
    raw = copy.deepcopy(item)
    for req_name in req_names:
        prop_val = parent_props.get(req_name, _MISSING)  # gjson Get on a materialized map
        if prop_val is _MISSING:
            return None, False
        _set_path(raw, "properties." + _escape_key(req_name), copy.deepcopy(prop_val))
    raw["type"] = "object"
    return raw, True


def _is_null_only_branch(item: Any) -> bool:
    """isNullOnlyBranch (gemini_schema.go:1621)."""
    if not isinstance(item, dict):
        return False
    if "properties" in item or "items" in item:
        return False
    t = item.get("type")
    if isinstance(t, str):
        return t == "null"
    if isinstance(t, list) and len(t) > 0:
        return all(_string_of(e) == "null" for e in t)
    return False


def _partition_union_branches(items: list, parent_props: Any):
    """partitionUnionBranches (gemini_schema.go:1640)."""
    non_null: List[Any] = []
    has_null = False
    has_filtered = False
    for item in items:
        if item is True:
            non_null.append({})
            has_filtered = True
            continue
        if not isinstance(item, dict):
            has_filtered = True
            continue
        if _is_null_only_branch(item):
            has_null = True
            continue
        if _is_required_only_union_branch(item):
            materialized, ok = _materialize_required_only_branch(item, parent_props)
            if ok:
                non_null.append(materialized)
                has_filtered = True
                continue
            has_filtered = True
            continue
        non_null.append(item)
    return non_null, has_null, has_filtered


def _select_best(items: list):
    """selectBest (gemini_schema.go:1829) -- object>array>typed scalar>null."""
    best_score = -1
    best_idx = -1
    types: List[str] = []
    for i, item in enumerate(items):
        t = _string_of(_get_child(item, "type"))
        score = 0
        if t == "object" or _get_child(item, "properties") is not _MISSING:
            score, t = 3, (t or "object")
        elif t == "array" or _get_child(item, "items") is not _MISSING:
            score, t = 2, (t or "array")
        elif t != "" and t != "null":
            score = 1
        elif t == "null":
            score, t = 0, "null"
        else:
            score, t = 0, ""
        if t != "":
            types.append(t)
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx, types


def _flatten_any_of_one_of(doc: Any, preserve_one_of: bool, flatten_multi_branch: bool) -> Any:
    """flattenAnyOfOneOf (gemini_schema.go:1669)."""
    by_field = _find_paths_by_fields(doc, ["oneOf", "anyOf"])
    paths: List[str] = []
    for key in ("oneOf", "anyOf"):  # Go iterates oneOf first
        paths.extend(by_field.get(key, []))
    _sort_by_depth(paths)

    for p in paths:
        key = "anyOf"
        if _split_path(p)[-1] == "oneOf":
            key = "oneOf"

        arr = _get_node(doc, p)
        if not isinstance(arr, list) or len(arr) == 0:
            continue

        parent_path = _trim_suffix(p, "." + key)
        if is_property_definition(parent_path):
            continue
        parent = doc if parent_path == "" else _get_node(doc, parent_path)
        if not isinstance(parent, dict):
            parent = {}
        parent_props = _get_child(parent, "properties")

        # anyOf+oneOf siblings on a native Schema carrier: keep anyOf, hint the oneOf
        # (skipped when the primary union is about to be flattened away too).
        if key == "oneOf" and not flatten_multi_branch and not preserve_one_of \
                and isinstance(parent.get("anyOf"), list):
            _append_hint(doc, parent_path, "oneOf: " + _raw(arr))
            _delete_path(doc, p)
            continue

        items = arr
        non_null_items, has_null, has_filtered = _partition_union_branches(items, parent_props)
        # A prior union-preserving pass may already have stripped the null branch and
        # recorded nullable:true on this node; the flattening must inherit that marker.
        nullable = has_null or _bool_of(_get_child(parent, "nullable"))

        if len(non_null_items) >= 2 and not flatten_multi_branch:
            target_key = key
            if key == "oneOf" and not preserve_one_of:
                target_key = "anyOf"  # native Schema carries anyOf only
            target_path = _join_path(parent_path, target_key)
            if target_key != key or has_null or has_filtered:
                branches = [copy.deepcopy(b) for b in non_null_items]
                _set_path(doc, target_path, branches)
                if target_key != key:
                    _delete_path(doc, p)
            if has_null:
                _set_path(doc, _join_path(parent_path, "nullable"), True)
                _append_hint(doc, parent_path, "(nullable)")
            continue

        # Parent already defines properties: merge branch properties in, never replace it.
        if isinstance(parent_props, dict):
            for item in non_null_items:
                branch_props = _get_child(item, "properties")
                if isinstance(branch_props, dict):
                    for prop_key, prop_val in branch_props.items():
                        dest = _join_path(parent_path, "properties." + _escape_key(prop_key))
                        _merge_missing_schema_at_path(doc, dest, prop_val)
            if nullable:
                _set_path(doc, _join_path(parent_path, "nullable"), True)
            if _string_of(_get_child(parent, "type")) == "":
                _set_path(doc, _join_path(parent_path, "type"), "object")
            _delete_path(doc, p)
            continue

        if len(non_null_items) == 0:
            if not has_null:
                _delete_path(doc, p)
                continue
            selected = {"type": "null"}
            parent_desc = _string_of(_get_node(doc, _description_path(parent_path)))
            if parent_desc != "":
                _merge_description_raw(selected, parent_desc)
            doc = _assign_at(doc, parent_path, selected)
            continue

        candidate_items: List[Any] = items
        if has_filtered or len(non_null_items) >= 2:
            candidate_items = list(non_null_items)
            if has_null:
                candidate_items = list(non_null_items) + [{"type": "null"}]

        parent_desc = _string_of(_get_node(doc, _description_path(parent_path)))
        _, all_types = _select_best(candidate_items)
        # Strongest non-null branch (ties keep the first one); selectBest scores
        # union-bearing branches 0, so a nested union never wins over a scalar here.
        selected = copy.deepcopy(non_null_items[0])
        best_idx, _ = _select_best(non_null_items)
        if 0 <= best_idx < len(non_null_items):
            selected = copy.deepcopy(non_null_items[best_idx])
        if nullable:
            selected = dict(selected)
            selected["nullable"] = True
        if parent_desc != "":
            selected = dict(selected)
            _merge_description_raw(selected, parent_desc)
        if len(all_types) > 1:
            selected = dict(selected)
            _append_hint_raw(selected, "Accepts: " + " | ".join(all_types))
        elif nullable:
            selected = dict(selected)
            _append_hint_raw(selected, "(nullable)")

        doc = _assign_at(doc, parent_path, selected)
    return doc


def _flatten_type_arrays(doc: Any, preserve_native_nullable: bool) -> Any:
    """flattenTypeArrays (gemini_schema.go:1858)."""
    paths = _find_paths(doc, "type")
    _sort_by_depth(paths)
    nullable_fields: dict = {}

    for p in paths:
        res = _get_node(doc, p)
        if not isinstance(res, list) or len(res) == 0:
            continue
        has_null = False
        non_null_types: List[str] = []
        for item in res:
            s = _string_of(item)
            if s == "null":
                has_null = True
            elif s != "":
                non_null_types.append(s)

        parent_path = _trim_suffix(p, ".type")
        first_type = "string"
        if non_null_types:
            if _exists(doc, _join_path(parent_path, "items")) and "array" in non_null_types:
                first_type = "array"
            elif _exists(doc, _join_path(parent_path, "properties")) and "object" in non_null_types:
                first_type = "object"
            else:
                first_type = non_null_types[0]
        _set_path(doc, p, first_type)

        if first_type != "array" and _exists(doc, _join_path(parent_path, "items")):
            _delete_path(doc, _join_path(parent_path, "items"))
        if len(non_null_types) > 1:
            _append_hint(doc, parent_path, "Accepts: " + " | ".join(non_null_types))

        if has_null:
            if preserve_native_nullable:
                _set_path(doc, _join_path(parent_path, "nullable"), True)
                _append_hint(doc, parent_path, "(nullable)")
                continue
            parts = _split_path(p)
            if len(parts) >= 3 and parts[len(parts) - 3] == "properties":
                field_name_escaped = parts[len(parts) - 2]
                field_name = _unescape_key(field_name_escaped)
                object_path = ".".join(parts[:len(parts) - 3])
                nullable_fields.setdefault(object_path, []).append(field_name)
                _append_hint(doc, _join_path(object_path, "properties." + field_name_escaped), "(nullable)")

    for object_path, fields in nullable_fields.items():
        req_path = _join_path(object_path, "required")
        req = _get_node(doc, req_path)
        if not isinstance(req, list):
            continue
        filtered = [_string_of(r) for r in req if _string_of(r) not in fields]
        if len(filtered) == 0:
            _delete_path(doc, req_path)
        else:
            _set_path(doc, req_path, filtered)
    return doc


def _remove_unsupported_keywords(doc: Any, options: JsonSchemaCleanOptions) -> Any:
    """removeUnsupportedKeywords (gemini_schema.go:1947)."""
    keywords = _constraint_keywords(options) + [
        "$schema", "$defs", "definitions", "const", "$ref", "$id", "id", "additionalProperties",
        "$anchor", "$vocabulary", "$dynamicRef", "$dynamicAnchor",
        "propertyNames", "patternProperties",  # Gemini doesn't support these schema keywords
        "dependentRequired", "dependentSchemas", "dependencies",
        "if", "then", "else",
        "$comment", "enumDescriptions", "enumTitles", "prefill", "deprecated",
        "encrypted", "discriminator", "readOnly", "writeOnly",
        "additionalItems", "unevaluatedProperties", "unevaluatedItems",
        "contentSchema", "contentMediaType", "contentEncoding",
    ]
    if options.antigravity_semantics:
        keywords = keywords + ["not"]

    delete_paths = []
    by_field = _find_paths_by_fields(doc, keywords)
    for key in keywords:
        for p in by_field.get(key, []):
            if is_property_definition(_trim_suffix(p, "." + key)):
                continue
            if key == "additionalProperties":
                if options.preserve_all_additional_properties:
                    continue
                if options.preserve_additional_properties_false and _get_node(doc, p) is False:
                    continue
            delete_paths.append(p)
    _sort_by_depth(delete_paths)
    for p in delete_paths:
        _delete_path(doc, p)
    # Remove x-* extension fields (e.g. x-google-enum-descriptions) Google APIs reject.
    return _remove_extension_fields(doc)


def _remove_extension_fields(doc: Any) -> Any:
    """removeExtensionFields (gemini_schema.go:1990)."""
    paths: List[str] = []
    _walk_for_extensions(doc, "", paths)
    for p in paths:
        _delete_path(doc, p)
    return doc


def _walk_for_extensions(value: Any, path: str, out: List[str]) -> None:
    """walkForExtensions (gemini_schema.go:2004) -- arrays walk in reverse index order."""
    if isinstance(value, list):
        for i in range(len(value) - 1, -1, -1):
            _walk_for_extensions(value[i], _join_path(path, str(i)), out)
        return
    if isinstance(value, dict):
        for key_str, val in value.items():
            child = _join_path(path, _escape_key(key_str))
            if key_str.startswith("x-") and not is_property_definition(path):
                out.append(child)
                continue  # extension field: delete wholesale, children not inspected
            if _is_literal_value_keyword(key_str) and not is_property_definition(path):
                continue  # literal values are client data
            _walk_for_extensions(val, child, out)


def _cleanup_required_fields(doc: Any) -> Any:
    """cleanupRequiredFields (gemini_schema.go:2045)."""
    for p in _find_paths(doc, "required"):
        parent_path = _trim_suffix(p, ".required")
        props_path = _join_path(parent_path, "properties")
        req = _get_node(doc, p)
        props = _get_node(doc, props_path)
        if not isinstance(req, list):
            continue
        if not isinstance(props, dict):
            _delete_path(doc, p)
            continue
        valid = [k for k in (_string_of(r) for r in req)
                 if _get_child(props, k) is not _MISSING]  # gjson Get(props, escaped key) == raw key lookup
        if len(valid) != len(req):
            if not valid:
                _delete_path(doc, p)
            else:
                _set_path(doc, p, valid)
    return doc


def _sanitize_object_properties(doc: Any) -> Any:
    """sanitizeObjectProperties (gemini_schema.go:196) -- properties implies type object."""
    paths = _find_paths(doc, "properties")
    _sort_by_depth(paths)
    for p in paths:
        parent_path = _trim_suffix(p, ".properties")
        if is_property_definition(parent_path):
            continue
        props = _get_node(doc, p)
        if not isinstance(props, dict):
            continue
        type_path = _join_path(parent_path, "type")
        if _string_of(_get_node(doc, type_path)).lower() != "object":
            _set_path(doc, type_path, "object")
    return doc


def _sanitize_array_items(doc: Any, add_missing: bool) -> Any:
    """sanitizeArrayItems (gemini_schema.go:220)."""
    paths = _find_paths(doc, "items")
    _sort_by_depth(paths)
    for p in paths:
        parent_path = _trim_suffix(p, ".items")
        if is_property_definition(parent_path):
            continue
        type_path = _join_path(parent_path, "type")
        t_str = _string_of(_get_node(doc, type_path))
        if t_str == "":
            _set_path(doc, type_path, "array")
        elif t_str.lower() != "array":
            _delete_path(doc, p)

    if not add_missing:
        return doc
    type_paths = _find_paths(doc, "type")
    _sort_by_depth(type_paths)
    for p in type_paths:
        parent_path = _trim_suffix(p, ".type")
        t = _get_node(doc, p)
        if is_property_definition(parent_path) or not _is_array_declared_type(t):
            continue
        if _exists(doc, _join_path(parent_path, "items")) or _exists(doc, _join_path(parent_path, "$ref")):
            continue
        _set_path(doc, _join_path(parent_path, "items"), {"type": "string"})
    return doc


def _remove_keywords(doc: Any, keywords) -> Any:
    """removeKeywords (gemini_schema.go:261)."""
    delete_paths = []
    by_field = _find_paths_by_fields(doc, keywords)
    for key in keywords:
        for p in by_field.get(key, []):
            if is_property_definition(_trim_suffix(p, "." + key)):
                continue
            delete_paths.append(p)
    _sort_by_depth(delete_paths)
    for p in delete_paths:
        _delete_path(doc, p)
    return doc


def _remove_placeholder_fields(doc: Any) -> Any:
    """removePlaceholderFields (gemini_schema.go:280) -- inverse of the placeholder pass.
    Go checks HasSuffix(p, ".properties._") / ".properties.reason", so a document-root
    placeholder is deliberately left alone there."""
    paths = _find_paths(doc, "_")
    _sort_by_depth(paths)
    for p in paths:
        if not p.endswith(".properties._"):
            continue
        _delete_path(doc, p)
        parent_path = p[: -len(".properties._")]
        req_path = _join_path(parent_path, "required")
        req = _get_node(doc, req_path)
        if isinstance(req, list):
            filtered = [_string_of(r) for r in req if _string_of(r) != "_"]
            if not filtered:
                _delete_path(doc, req_path)
            else:
                _set_path(doc, req_path, filtered)

    reason_paths = _find_paths(doc, "reason")
    _sort_by_depth(reason_paths)
    for p in reason_paths:
        if not p.endswith(".properties.reason"):
            continue
        parent_path = p[: -len(".properties.reason")]
        props = _get_node(doc, _join_path(parent_path, "properties"))
        if not isinstance(props, dict) or len(props) != 1:
            continue
        desc = _get_node(doc, p + ".description")
        if desc != PLACEHOLDER_REASON_DESCRIPTION:
            continue
        _delete_path(doc, p)
        req_path = _join_path(parent_path, "required")
        req = _get_node(doc, req_path)
        if isinstance(req, list):
            filtered = [_string_of(r) for r in req if _string_of(r) != "reason"]
            if not filtered:
                _delete_path(doc, req_path)
            else:
                _set_path(doc, req_path, filtered)
    return doc


def _add_empty_schema_placeholder(doc: Any) -> Any:
    """addEmptySchemaPlaceholder (gemini_schema.go:2082) -- Claude VALIDATED requirement:
    'reason' (string + CPA description) for property-less objects, '_' (boolean) for
    objects that have properties but no required entries (never at the document root)."""
    paths = _find_paths(doc, "type")
    _sort_by_depth(paths)
    for p in paths:
        if _string_of(_get_node(doc, p)) != "object":
            continue
        parent_path = _trim_suffix(p, ".type")
        props_path = _join_path(parent_path, "properties")
        props_val = _get_node(doc, props_path)
        req_path = _join_path(parent_path, "required")
        req_val = _get_node(doc, req_path)
        has_required = isinstance(req_val, list) and len(req_val) > 0
        if isinstance(_get_node(doc, _join_path(parent_path, "anyOf")), list) \
                or isinstance(_get_node(doc, _join_path(parent_path, "oneOf")), list):
            continue

        needs_placeholder = props_val is _MISSING or (isinstance(props_val, dict) and len(props_val) == 0)
        if needs_placeholder:
            reason_path = _join_path(props_path, "reason")
            _set_path(doc, reason_path + ".type", "string")
            _set_path(doc, reason_path + ".description", PLACEHOLDER_REASON_DESCRIPTION)
            _set_path(doc, req_path, ["reason"])
            continue

        # properties exist but none required: add the minimal '_' placeholder.
        if isinstance(props_val, dict) and not has_required:
            if parent_path == "":
                continue  # never synthesize '_' at the top-level schema
            placeholder_path = _join_path(props_path, "_")
            if not _exists(doc, placeholder_path):
                _set_path(doc, placeholder_path + ".type", "boolean")
            _set_path(doc, req_path, ["_"])
    return doc


# --- the pipeline ------------------------------------------------------------------

def clean_json_schema(schema: Any, options: JsonSchemaCleanOptions) -> Any:
    """cleanJSONSchema (gemini_schema.go:134-194) -- stage-for-stage. The input is
    deep-copied; the caller's object is never mutated."""
    doc = _normalize_numbers(copy.deepcopy(schema))

    # Phase 0: Normalize malformed schemas (bare property maps, boolean required).
    # Array placeholders are deferred to sanitizeArrayItems after refs/allOf resolve.
    doc = _normalize_malformed_schema_objects(doc)

    # Phase 1: Convert and add hints; refs must be expanded before $defs/definitions die.
    doc = _inline_local_refs_for_clean(doc)
    doc = _convert_refs_to_hints(doc, options.antigravity_semantics)
    doc = _convert_const_to_enum(doc)
    doc = _convert_enum_values_to_strings(doc, options.force_enum_string_type)
    doc = _add_enum_hints(doc)
    doc = _drop_ignored_enums_to_hints(doc, options)
    if not options.preserve_additional_properties_false and not options.preserve_all_additional_properties:
        doc = _add_additional_properties_hints(doc)
    doc = _move_constraints_to_description(doc, options)
    if options.antigravity_semantics:
        doc = _move_not_to_description(doc)

    # Phase 2: Flatten complex structures. Once a conjunction exists, local shape
    # assumptions are unsafe in ancestors, siblings, and descendants.
    preserve_conjunctions = options.preserve_standard_constraints and _has_schema_conjunction(doc)
    doc = _merge_conditionals(doc)
    if not options.preserve_standard_constraints:
        doc = _merge_all_of(doc)
    if not preserve_conjunctions:
        if options.flatten_unions:
            doc = _flatten_any_of_one_of(doc, options.preserve_standard_constraints,
                                         options.flatten_multi_branch_unions)
        doc = _flatten_type_arrays(doc, options.antigravity_semantics)

    # Phase 3: Cleanup
    doc = _remove_unsupported_keywords(doc, options)
    if options.remove_gemini_metadata:
        doc = _remove_keywords(doc, ["nullable", "title"])
        doc = _remove_placeholder_fields(doc)
    elif options.remove_tool_title:
        doc = _remove_keywords(doc, ["title"])
    if not preserve_conjunctions:
        doc = _cleanup_required_fields(doc)
        doc = _sanitize_object_properties(doc)
        doc = _sanitize_array_items(doc, options.add_missing_array_items)

    # Phase 4: Add placeholder for empty object schemas (Claude VALIDATED mode requirement)
    if options.add_placeholder:
        doc = _add_empty_schema_placeholder(doc)

    return _demote_numbers(doc)


# --- public presets ------------------------------------------------------------------

def antigravity_tool_schema(schema: dict, *, require_placeholder: bool,
                            flatten_multi_branch_unions: bool) -> dict:
    """CleanJSONSchemaForAntigravityTool / ...ClaudeTool (gemini_schema.go:60-72).
    requirePlaceholder is only set for Claude VALIDATED mode -- the Antigravity
    executor passes its useAntigravitySchema flag (true for Claude models and the
    gemini-3 pro family; see antigravity_executor_request.go:72,240-242). The
    Claude variant additionally flattens multi-branch anyOf/oneOf unions (Issue 3662)."""
    return clean_json_schema(schema, _antigravity_tool_options(require_placeholder,
                                                                flatten_multi_branch_unions))


def clean_antigravity_claude_tool_schema(schema: dict, require_placeholder: bool) -> dict:
    """CleanJSONSchemaForAntigravityClaudeTool with its explicit placeholder flag (line 70)."""
    return antigravity_tool_schema(schema, require_placeholder=require_placeholder,
                                   flatten_multi_branch_unions=True)


def clean_antigravity_tool(schema: dict) -> dict:
    """Gemini-on-Antigravity preset (CleanJSONSchemaForAntigravityTool, false, line 60):
    require_placeholder=False, multi-branch unions preserved."""
    return antigravity_tool_schema(schema, require_placeholder=False,
                                   flatten_multi_branch_unions=False)


def clean_antigravity_claude_tool(schema: dict) -> dict:
    """Claude VALIDATED preset (CleanJSONSchemaForAntigravityClaudeTool, true, lines 70-72
    as used by the executor with useAntigravitySchema=true for Claude models):
    placeholder required, multi-branch unions flattened (Issue 3662)."""
    return antigravity_tool_schema(schema, require_placeholder=True,
                                   flatten_multi_branch_unions=True)


def clean_antigravity_response(schema: dict) -> dict:
    """CleanJSONSchemaForAntigravityResponse (gemini_schema.go:98)."""
    return clean_json_schema(schema, _ANTIGRAVITY_RESPONSE_OPTIONS)


def clean_gemini(schema: Any) -> Any:
    """CleanJSONSchemaForGemini (gemini_schema.go:109)."""
    return clean_json_schema(schema, _GEMINI_OPTIONS)


def clean_gemini_json_schema(schema: Any) -> Any:
    """CleanJSONSchemaForGeminiJSONSchema (gemini_schema.go:122)."""
    return clean_json_schema(schema, _GEMINI_JSON_SCHEMA_OPTIONS)
