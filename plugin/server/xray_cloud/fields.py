from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import FieldSpec, ProjectSettings
from .errors import ToolError


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _convert(spec: FieldSpec, value: Any) -> Any:
    t = spec.type
    if t == "option":
        return value if isinstance(value, dict) else {"value": value}
    if t == "options":
        return [v if isinstance(v, dict) else {"value": v} for v in _as_list(value)]
    if t == "version":
        return value if isinstance(value, dict) else {"name": value}
    if t == "versions":
        return [v if isinstance(v, dict) else {"name": v} for v in _as_list(value)]
    if t == "user":
        return value if isinstance(value, dict) else {"accountId": value}
    if t == "number" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                raise ToolError("Field %r expects a number" % spec.name, field=spec.name)
    return value


convert_value = _convert  # FieldSpec value -> Jira value


def _resolve(project: ProjectSettings, item_type: str, fields: Optional[Dict[str, Any]]):
    """Return {friendly name: converted value} after defaults; checks unknown names."""
    specs = project.fields.get(item_type) or {}
    fields = fields or {}
    unknown = [n for n in fields if n not in specs]
    if unknown:
        raise ToolError("Unknown field name(s) for %s: %s" % (item_type, ", ".join(unknown)),
                        unknownFields=unknown, knownFields=list(specs))
    values = {}
    for name, spec in specs.items():
        if name in fields:
            values[name] = fields[name]
        elif spec.default is not None:
            values[name] = spec.default
    return specs, values


def _check_required(specs: Dict[str, FieldSpec], values: dict, satisfied_by_raw) -> None:
    missing = []
    for name, spec in specs.items():
        if spec.required and name not in values and not satisfied_by_raw(spec):
            missing.append({"name": name, "id": spec.id or spec.xray_name})
    if missing:
        raise ToolError("Missing required fields", missingFields=missing)


def build_jira_fields(project: ProjectSettings, item_type: str, fields: Optional[Dict[str, Any]],
                      raw_fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    specs, values = _resolve(project, item_type, fields)
    raw = dict(raw_fields or {})
    _check_required(specs, values, lambda s: s.id in raw)
    out: Dict[str, Any] = {specs[n].id: _convert(specs[n], v) for n, v in values.items()}
    out.update(raw)
    return out


def build_run_fields(project: ProjectSettings, fields: Optional[Dict[str, Any]],
                     raw_fields: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs, values = _resolve(project, "testRun", fields)
    raw = dict(raw_fields or {})
    _check_required(specs, values, lambda s: (s.xray_name or s.id) in raw)
    out: List[Dict[str, Any]] = []
    for n, v in values.items():
        s = specs[n]
        key = (s.xray_name or s.id)
        if key in raw:
            continue
        entry = {"name": s.xray_name} if s.xray_name else {"id": s.id}
        entry["value"] = _convert(s, v)
        out.append(entry)
    out.extend({"name": k, "value": v} for k, v in raw.items())
    return out
