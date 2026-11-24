from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from .approval import ALLOW_PROJECT_PARAM
from .context import is_permission_error, permission_denied, require_write
from .errors import NeedsAnswer, ToolError


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    handler: Callable[[Any, dict], Any]
    write: bool = False


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _types(schema: dict) -> List[str]:
    t = schema.get("type")
    if t is None:
        return []
    return list(t) if isinstance(t, list) else [t]


def _matches(value: Any, types: List[str]) -> bool:
    return any(_TYPE_CHECKS[t](value) for t in types)


def _coerce_value(value: Any, schema: dict, path: str) -> Any:
    types = _types(schema)
    if types and not _matches(value, types) and isinstance(value, str):
        if "array" in types or "object" in types:
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            else:
                if _matches(parsed, types):
                    value = parsed
        if isinstance(value, str) and "integer" in types:
            try:
                value = int(value.strip())
            except ValueError:
                pass
    if types and not _matches(value, types):
        raise ToolError("Invalid type for '%s': expected %s" % (path, " or ".join(types)))
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError("Invalid value for '%s': must be one of %s" % (path, schema["enum"]))
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            raise ToolError("Invalid value for '%s': must be >= %s" % (path, schema["minimum"]))
    if "maximum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > schema["maximum"]:
            raise ToolError("Invalid value for '%s': must be <= %s" % (path, schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ToolError("Invalid value for '%s': needs at least %s item(s)" % (path, schema["minItems"]))
        if isinstance(schema.get("items"), dict):
            value = [_coerce_value(v, schema["items"], "%s[%d]" % (path, i)) for i, v in enumerate(value)]
    elif isinstance(value, dict) and ("properties" in schema or "required" in schema):
        value = _coerce_object(value, schema, path)
    return value


def _coerce_object(args: dict, schema: dict, path: str = "") -> dict:
    props = schema.get("properties", {})
    prefix = path + "." if path else ""
    for key in schema.get("required", []):
        if key not in args:
            raise ToolError("Missing required parameter '%s%s'" % (prefix, key))
    # Top-level unknown keys are always rejected so typos surface; nested ones need additionalProperties: false.
    if props and (schema.get("additionalProperties") is False or not path):
        unknown = sorted(k for k in args if k not in props)
        if unknown:
            raise ToolError("Unknown parameter '%s%s'" % (prefix, unknown[0]), unknown=unknown)
    required = set(schema.get("required", []))
    out = {}
    for key, value in args.items():
        if (value is None and key in props and key not in required
                and "null" not in _types(props[key])):
            continue  # null = omitted (clients send null for parameters they leave unset)
        out[key] = _coerce_value(value, props[key], prefix + key) if key in props else value
    return out


def coerce(args: dict, schema: dict) -> dict:
    """Coerce (JSON strings, numeric strings) and validate args against the schema subset."""
    if not isinstance(args, dict):
        raise ToolError("Tool arguments must be an object")
    return _coerce_object(args, schema)


def _error_result(request_data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(request_data, ensure_ascii=False)}], "isError": True}


class Registry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self.allowed: Optional[Set[str]] = None  # None = every tool (XRAY_TOOLS unset)

    def tool(self, name: str, description: str, params: dict, write: bool = False):
        if write:  # every write tool takes the user's answer to the project approval question
            params = dict(params, properties=dict(params.get("properties") or {},
                                                  allowProject=ALLOW_PROJECT_PARAM))

        def decorator(fn):
            self._tools[name] = Tool(name, description, params, fn, write)
            return fn

        return decorator

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def list(self) -> List[dict]:
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.params}
            for t in self._tools.values() if self.allowed is None or t.name in self.allowed
        ]

    def call(self, ctx: Any, name: str, args: dict) -> dict:
        try:
            tool = self._tools.get(name)
            if tool is None:
                raise ToolError("Unknown tool '%s'" % name)
            if self.allowed is not None and name not in self.allowed:
                raise ToolError("Tool '%s' is not enabled: it is outside the tool groups selected by "
                                "XRAY_TOOLS. Add its group to XRAY_TOOLS or unset it." % name)
            clean = coerce(args or {}, tool.params)
            if tool.write:
                require_write(ctx)
            # allowProject is read by require_project (via ctx.call_args), not by handlers.
            if tool.write:
                ctx.call_args = {"allowProject": clean.pop("allowProject", None)}
            try:
                result = tool.handler(ctx, clean)
            except ToolError as e:
                if tool.write and is_permission_error(e):
                    raise permission_denied(e, action="run %s" % name,
                                            project_key=clean.get("projectKey")) from None
                raise
            finally:
                if tool.write:
                    ctx.call_args = {}
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except NeedsAnswer as e:  # a question, not an error: nothing was written
            return {"content": [{"type": "text",
                                 "text": json.dumps(e.to_dict(), ensure_ascii=False)}]}
        except ToolError as e:
            return _error_result(e.to_dict())
        except Exception as e:  # never leak a traceback to stdout
            print("tool %s failed: %r" % (name, e), file=sys.stderr)
            return _error_result({"error": "Internal error in %s: %s" % (name, e)})


REGISTRY = Registry()
