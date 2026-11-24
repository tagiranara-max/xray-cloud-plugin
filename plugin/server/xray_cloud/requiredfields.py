"""Jira fields of a new item: the call's fields/rawFields, then settings specs and defaults,
then remembered values, then required fields discovered from Jira create-meta (with the Jira
token). A required field with no value is asked: by elicitation when the client supports it
and every missing field fits a flat form, else by needs_answer. Answers are remembered per
user, site, project, item type and field, when the write goes ahead (commit)."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from .config import FieldSpec, ProjectSettings
from .errors import NeedsAnswer, ToolError
from .fields import convert_value

ITEM_TYPE_NAMES = {"test": "Test", "testExecution": "Test Execution", "testPlan": "Test Plan",
                   "testSet": "Test Set", "precondition": "Precondition"}
SYSTEM_FIELDS = frozenset({"summary", "project", "issuetype", "reporter"})
MAX_CHOICES = 50
# create-meta schema types a flat elicitation form can hold (one primitive value)
_FORM_TYPES = frozenset({"string", "number", "date", "datetime", "option", "priority",
                         "version", "component", "securitylevel", "resolution"})
_NAME_TYPES = frozenset({"priority", "version", "component", "securitylevel", "resolution"})
_FORM_SPEC_TYPES = frozenset({"string", "number", "option", "version"})


class Resolution(NamedTuple):
    fields: Dict[str, Any]     # Jira field id -> value, as sent
    applied: Dict[str, Any]    # remembered values that were used
    missing: List[dict]        # required fields without a value
    answered: Dict[str, Any]   # required fields the call itself supplied


# -- create-meta ------------------------------------------------------------

def _meta(ctx: Any, pk: str, item_type: str) -> Optional[List[dict]]:
    """create-meta fields of the item type, cached per server run; None without the Jira
    token, for an unknown item type, or when Jira could not be read (then nothing is
    discovered and the create goes ahead as without the token)."""
    name = ITEM_TYPE_NAMES.get(item_type)
    jira = getattr(ctx, "jira", None)
    if name is None or jira is None:
        return None
    cache = getattr(ctx, "create_meta_cache", None)
    if cache is None:
        cache = ctx.create_meta_cache = {}
    key = (pk, item_type)
    if key not in cache:
        try:
            meta = jira.create_meta(pk, name)
        except ToolError:
            return None
        cache[key] = [f for f in meta or [] if isinstance(f, dict) and f.get("id")]
    return cache[key]


def _lookup(meta: Optional[List[dict]], name: str) -> Optional[dict]:
    if not meta:
        return None
    for f in meta:
        if f["id"] == name:
            return f
    folded = [f for f in meta if str(f.get("name") or "").casefold() == name.casefold()]
    return folded[0] if len(folded) == 1 else None


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _one(t: Optional[str], value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value
    if t == "option":
        return {"value": value}
    if t in _NAME_TYPES:
        return {"name": value}
    if t == "user":
        return {"accountId": value}
    if t == "number" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                raise ToolError("Field %r expects a number" % name, field=name) from None
    return value


def _convert_meta(f: dict, value: Any) -> Any:
    name = f.get("name") or f["id"]
    if f.get("schemaType") == "array":
        return [_one(f.get("schemaItems"), v, name) for v in _as_list(value)]
    return _one(f.get("schemaType"), value, name)


# -- answers pending until the write ----------------------------------------

def reset(ctx: Any) -> None:
    """Forget answers not yet committed (start and end of every tool call)."""
    try:
        ctx.pending_fields = {}
    except AttributeError:
        pass


def _pending(ctx: Any) -> Dict[Tuple[str, str], dict]:
    pending = getattr(ctx, "pending_fields", None)
    if pending is None:
        pending = {}
        ctx.pending_fields = pending
    return pending


def _add_pending(ctx: Any, pk: str, item_type: str, values: Dict[str, Any],
                 remember: bool) -> None:
    if not values:
        return
    entry = _pending(ctx).setdefault((pk, item_type), {"values": {}, "remember": False})
    entry["values"].update(values)
    entry["remember"] = entry["remember"] or bool(remember)


def commit(ctx: Any) -> None:
    """Remember this call's answers (all validation passed; the write comes next). A field
    is stored when the call asked to remember, or when nothing is remembered for it yet."""
    pending = _pending(ctx)
    site = getattr(ctx, "site", "")
    if pending and site:
        from .choices import region_of
        region = region_of(ctx)
        for (pk, item_type), entry in pending.items():
            known = ctx.preferences.field_values(region, site, pk, item_type)
            store = {k: v for k, v in entry["values"].items()
                     if entry["remember"] or k not in known}
            ctx.preferences.remember_fields(region, site, pk, item_type, store)
    reset(ctx)


def _remembered(ctx: Any, pk: str, item_type: str) -> Dict[str, Any]:
    site = getattr(ctx, "site", "")
    if not site:
        return {}
    from .choices import region_of
    return ctx.preferences.field_values(region_of(ctx), site, pk, item_type)


# -- resolution -------------------------------------------------------------

def _field(fid: str, name: str, ftype: Optional[str], allowed: list,
           spec: Optional[FieldSpec] = None, meta: Optional[dict] = None) -> dict:
    return {"id": fid, "name": name, "type": ftype, "allowedValues": list(allowed or []),
            "spec": spec, "meta": meta}


def compute(ctx: Any, project: ProjectSettings, item_type: str,
            fields: Optional[Dict[str, Any]], raw_fields: Optional[Dict[str, Any]],
            provided: Iterable[str] = ()) -> Resolution:
    """The fields of a new item, without asking. `provided`: ids the tool sets itself."""
    pk = project.key
    specs = project.fields.get(item_type) or {}
    meta = _meta(ctx, pk, item_type)
    by_id = {s.id: s for s in specs.values() if s.id}
    call: Dict[str, Any] = {}
    unknown = []
    for name, value in (fields or {}).items():
        spec = specs.get(name) or by_id.get(name)
        if spec is not None:
            call[spec.id] = convert_value(spec, value)
            continue
        f = _lookup(meta, name)
        if f is None:
            unknown.append(name)
        else:
            call[f["id"]] = _convert_meta(f, value)
    if unknown:
        known = list(specs) + [f.get("name") or f["id"] for f in meta or []
                               if f["id"] not in SYSTEM_FIELDS and f["id"] not in by_id]
        raise ToolError("Unknown field name(s) for %s: %s" % (item_type, ", ".join(unknown)),
                        unknownFields=unknown, knownFields=known)
    call.update(raw_fields or {})
    defaults = {s.id: convert_value(s, s.default) for s in specs.values()
                if s.id and s.default is not None}
    remembered = _remembered(ctx, pk, item_type)
    if meta is not None:  # a field no longer on the create screen would fail the create
        ids = {f["id"] for f in meta}
        remembered = {k: v for k, v in remembered.items() if k in ids}
    answers = (_pending(ctx).get((pk, item_type)) or {}).get("values") or {}
    out = dict(remembered)
    out.update(defaults)
    out.update(answers)
    out.update(call)
    applied = {k: v for k, v in remembered.items()
               if k not in defaults and k not in answers and k not in call}
    meta_by_id = {f["id"]: f for f in meta or []}
    missing: List[dict] = []
    required = set()
    for name, s in specs.items():
        if s.required and s.id:
            required.add(s.id)
            if s.id not in out:
                m = meta_by_id.get(s.id) or {}
                missing.append(_field(s.id, name, s.type, m.get("allowedValues"), spec=s))
    skip = SYSTEM_FIELDS | set(provided)
    for f in meta or []:
        fid = f["id"]
        if not f.get("required") or fid in skip:
            continue
        required.add(fid)
        if fid not in out and fid not in by_id and not f.get("hasDefaultValue"):
            missing.append(_field(fid, f.get("name") or fid, f.get("schemaType"),
                                  f.get("allowedValues"), meta=f))
    answered = {k: v for k, v in call.items() if k in required}
    return Resolution(out, applied, missing, answered)


def _formable(f: dict) -> bool:
    if f["spec"] is not None:
        return f["spec"].type in _FORM_SPEC_TYPES
    return f["type"] in _FORM_TYPES


def _convert_answer(f: dict, value: Any) -> Any:
    if f["spec"] is not None:
        return convert_value(f["spec"], value)
    return _convert_meta(f["meta"], value)


def _type_label(f: dict) -> str:
    meta = f["meta"] or {}
    if f["type"] == "array" and meta.get("schemaItems"):
        return "array of %s" % meta["schemaItems"]
    return str(f["type"] or "any")


def questions(project: ProjectSettings, item_type: str, missing: List[dict],
              param: str) -> List[dict]:
    """needs_answer questions for missing required fields: name, id, type, first 50 allowed
    values."""
    tname = ITEM_TYPE_NAMES.get(item_type, item_type)
    out = []
    for f in missing:
        q: Dict[str, Any] = {
            "id": f["id"], "name": f["name"], "type": _type_label(f), "param": param,
            "question": "Value of the required field %s (%s) for a new %s in %s?"
                        % (f["name"], f["id"], tname, project.key),
            "context": {"projectKey": project.key, "itemType": item_type}}
        allowed = f["allowedValues"]
        if allowed:
            q["choices"] = allowed[:MAX_CHOICES]
            if len(allowed) > MAX_CHOICES:
                q["moreChoices"] = len(allowed) - MAX_CHOICES
        out.append(q)
    return out


def _form(missing: List[dict]) -> dict:
    props: Dict[str, Any] = {}
    for f in missing:
        t = f["spec"].type if f["spec"] is not None else f["type"]
        p: Dict[str, Any] = {"type": "number" if t == "number" else "string",
                             "title": f["name"],
                             "description": "Required Jira field %s" % f["id"]}
        if t == "date":
            p["format"] = "date"
        elif t == "datetime":
            p["format"] = "date-time"
        allowed = f["allowedValues"]
        if allowed and len(allowed) <= MAX_CHOICES and t != "number":
            p["enum"] = [str(v) for v in allowed]
        props[f["id"]] = p
    return {"type": "object", "properties": props, "required": [f["id"] for f in missing]}


def _missing_data(missing: List[dict]) -> List[dict]:
    return [{"name": f["name"], "id": f["id"]} for f in missing]


def ask(ctx: Any, project: ProjectSettings, item_type: str, missing: List[dict],
        param: str = "fields") -> Dict[str, Any]:
    """Answers {field id: Jira value} for the missing fields, from an elicitation form; or
    raises NeedsAnswer (no elicitation, or a field a flat form cannot hold) or ToolError
    (declined, cancelled or no answer: nothing is written or remembered)."""
    pk, tname = project.key, ITEM_TYPE_NAMES.get(item_type, item_type)
    names = ", ".join(f["name"] for f in missing)
    channel = getattr(ctx, "channel", None)
    if channel is not None and channel.supports_elicitation and all(map(_formable, missing)):
        content = channel.elicit(
            "A new %s in Jira project %s needs values for required fields: %s. The answers are "
            "remembered for your user as defaults for new %s items in %s."
            % (tname, pk, names, tname, pk), _form(missing))
        answers: Dict[str, Any] = {}
        for f in missing:
            value = content.get(f["id"]) if isinstance(content, dict) else None
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ToolError("No value was given for the required field(s) %s of the new %s "
                                "in %s; nothing was written and nothing was remembered."
                                % (names, tname, pk),
                                projectKey=pk, missingFields=_missing_data(missing))
            answers[f["id"]] = _convert_answer(f, value)
        return answers
    raise NeedsAnswer(
        "A new %s in %s needs values for required Jira fields: %s. Ask the user, then call "
        "again with `%s` set to {field id or name: value} (lists for multi-value fields, "
        "account ids for users); never answer it yourself. Nothing was written."
        % (tname, pk, names, param),
        questions(project, item_type, missing, param),
        projectKey=pk, missingFields=_missing_data(missing))


def resolve(ctx: Any, project: ProjectSettings, item_type: str,
            fields: Optional[Dict[str, Any]], raw_fields: Optional[Dict[str, Any]],
            remember: bool = False, *, param: str = "fields", provided: Iterable[str] = (),
            record: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """-> (Jira fields, applied remembered values). Asks for missing required fields (see
    ask). Answers (asked, or required fields given in the call when `record`) are remembered
    on commit(ctx): when `remember`, or when nothing is remembered yet for that field."""
    provided = tuple(provided)
    r = compute(ctx, project, item_type, fields, raw_fields, provided)
    if r.missing:
        _add_pending(ctx, project.key, item_type,
                     ask(ctx, project, item_type, r.missing, param), remember)
        r = compute(ctx, project, item_type, fields, raw_fields, provided)
    if record:
        _add_pending(ctx, project.key, item_type, r.answered, remember)
    return r.fields, r.applied


def add_applied(out: Dict[str, Any], item_type: str, applied: Dict[str, Any]) -> None:
    """Report remembered values a create used: out["appliedDefaults"][itemType]."""
    if applied:
        out.setdefault("appliedDefaults", {}).setdefault(item_type, {}).update(applied)
