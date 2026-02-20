import json
import logging
from typing import Any, Dict, List, Optional

from proxy_config import (
    DEBUG_UPSTREAM,
    DROP_NON_IDENTIFIER_TOOL_PROPS,
    IDENTIFIER_RE,
    UPSTREAM_TOOLS_STYLE,
)


logger = logging.getLogger("claude_to_openai_proxy")

_SAFE_EMPTY_STRING_KEYS = {
    # Keys that legitimately accept string values in JSON Schema.
    "description",
    "title",
    "pattern",
    "format",
    "$ref",
    "$comment",
    "comment",
    "contentEncoding",
    "contentMediaType",
}


def _schema_has_suspicious_empty_string(x: Any, parent_key: Optional[str] = None) -> bool:
    """
    Detect empty-string values in places that are likely to be treated as schemas by upstream validators.
    """
    if isinstance(x, dict):
        for k, v in x.items():
            if v == "" and k not in _SAFE_EMPTY_STRING_KEYS:
                return True
            if _schema_has_suspicious_empty_string(v, k):
                return True
        return False
    if isinstance(x, list):
        # Empty string inside enum is fine; elsewhere it often breaks schema validators.
        if parent_key == "enum":
            return False
        for item in x:
            if item == "":
                return True
            if _schema_has_suspicious_empty_string(item, parent_key):
                return True
        return False
    return False


def _fallback_parameters_schema() -> Dict[str, Any]:
    return {"type": "object", "properties": {}}


def _fallback_grep_schema() -> Dict[str, Any]:
    # Minimal, identifier-only schema that still matches common Grep tool inputs.
    return {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The regex pattern to search for."},
            "path": {"type": "string", "description": "File or directory to search in."},
            "glob": {"type": "string", "description": "Glob filter for files (e.g. \"*.py\")."},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Output mode.",
            },
        },
        "required": ["pattern"],
    }


def _coerce_schema_obj(x: Any) -> Dict[str, Any]:
    """
    Ensure we return a dict JSON schema.

    - If x is a dict: return as-is.
    - If x is a JSON string: parse into dict if possible.
    - Otherwise: return a minimal object schema.
    """
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {"type": "object", "properties": {}}
        try:
            j = json.loads(s)
            return j if isinstance(j, dict) else {"type": "object", "properties": {}}
        except Exception:
            return {"type": "object", "properties": {}}
    return {"type": "object", "properties": {}}


_DROP_KEYS = {
    "$schema",
    "$id",
    "id",
    # meta fields that commonly break stricter upstream validators
    "title",
    "examples",
    "default",
}

_SCHEMA_MAP_KEYS = {"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"}
_SCHEMA_LIST_KEYS = {"anyOf", "oneOf", "allOf", "prefixItems"}
_SCHEMA_SINGLE_KEYS = {
    "additionalProperties",
    "unevaluatedProperties",
    "items",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
}


def _sanitize_schema(x: Any) -> Any:
    """
    Compatibility cleanup for tool JSON Schemas.

    We have seen Claude/other tool definitions containing invalid values like
    empty-string schema nodes (e.g. `items: ""` or `properties: {"-C": ""}`),
    which can cause upstream 400 "Invalid schema ... '' is not valid under any of the given schemas".
    """
    if isinstance(x, dict):
        out: Dict[str, Any] = {}
        for k, v in x.items():
            if k in _DROP_KEYS:
                continue

            # Some schema keywords expect "schema | bool" or "schema" or a map-of-schemas.
            if v == "":
                if k in ("additionalProperties", "unevaluatedProperties"):
                    out[k] = True
                    continue
                if k in _SCHEMA_MAP_KEYS:
                    out[k] = {}
                    continue
                if k in _SCHEMA_LIST_KEYS:
                    out[k] = []
                    continue
                if k in _SCHEMA_SINGLE_KEYS:
                    out[k] = {}
                    continue
                if k == "type":
                    # Empty type is invalid; drop it.
                    continue
                # For most other keys, dropping is safer than keeping an invalid empty string.
                if k == "description":
                    out[k] = ""
                continue

            # Map-of-schemas keywords (properties, $defs, ...)
            if k in _SCHEMA_MAP_KEYS and isinstance(v, dict):
                cleaned_map: Dict[str, Any] = {}
                for pk, pv in v.items():
                    if pv == "":
                        cleaned_map[pk] = {}
                    else:
                        cleaned_map[pk] = _sanitize_schema(pv)
                out[k] = cleaned_map
                continue

            # List-of-schemas keywords (anyOf/oneOf/allOf)
            if k in _SCHEMA_LIST_KEYS and isinstance(v, list):
                cleaned_list: List[Any] = []
                for item in v:
                    if item in ("", None):
                        continue
                    # A schema must be object/bool; drop other primitives.
                    if not isinstance(item, (dict, bool)):
                        continue
                    cleaned_list.append(_sanitize_schema(item))
                out[k] = cleaned_list
                continue

            out[k] = _sanitize_schema(v)
        return out

    if isinstance(x, list):
        return [_sanitize_schema(i) for i in x if i not in ("", None)]

    return x


def anthropic_tools_to_openai_tools(req: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    tools = req.get("tools")
    if not tools:
        return None

    out: List[Dict[str, Any]] = []
    for t in tools:
        name = t.get("name")
        raw_schema = _coerce_schema_obj(t.get("input_schema"))
        clean_schema = _sanitize_schema(raw_schema)
        if not isinstance(clean_schema, dict):
            clean_schema = _fallback_parameters_schema()

        # Filter parameter names that stricter upstreams reject (e.g. "-A", "-B", "-n").
        if DROP_NON_IDENTIFIER_TOOL_PROPS:
            props = clean_schema.get("properties")
            if isinstance(props, dict):
                new_props: Dict[str, Any] = {}
                dropped: List[str] = []
                for k, v in props.items():
                    if IDENTIFIER_RE.match(k):
                        new_props[k] = v
                    else:
                        dropped.append(k)
                if dropped and DEBUG_UPSTREAM:
                    logger.debug("[tool-schema] %s dropped non-identifier props: %s", t.get("name"), dropped)

                clean_schema["properties"] = new_props

                # Keep required consistent, otherwise some validators fail again.
                if isinstance(clean_schema.get("required"), list):
                    clean_schema["required"] = [r for r in clean_schema["required"] if r in new_props]

        desc = (t.get("description", "") or "")

        # Last-resort fallback: if we still see suspicious empty-string nodes, send a minimal schema.
        # This avoids upstream 400 "'' is not valid under any of the given schemas".
        if name == "Grep":
            # Grep is frequently the offending one; keep it stable and strict.
            clean_schema = _fallback_grep_schema()
        elif _schema_has_suspicious_empty_string(clean_schema):
            if DEBUG_UPSTREAM:
                logger.debug("[tool-schema] %s fallback to minimal schema due to empty-string nodes", name)
            clean_schema = _fallback_parameters_schema()

        if UPSTREAM_TOOLS_STYLE == "flat":
            out.append(
                {
                    "name": name,
                    "description": desc,
                    "parameters": clean_schema,
                }
            )
        else:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": clean_schema,
                    },
                }
            )

    return out
