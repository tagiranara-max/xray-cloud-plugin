"""Jira fields of a new item: the call's fields/rawFields, then settings specs and defaults,
then remembered values, then required fields discovered from Jira create-meta (with the Jira
token). A required field with no value is asked: by elicitation when the client supports it
and every missing field fits a flat form, else by needs_answer. Answers are remembered per
user, site, project, item type and field, when the write goes ahead (commit).

Without the token nothing is discovered up front: the create is tried, and when Jira rejects
it for missing required fields (Xray creates nothing then), the fields are parsed from the
error and learned for the server run (`creating`), and the registry runs the call once more,
which asks for them like discovered ones."""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

from .config import FieldSpec, ProjectSettings
from .errors import NeedsAnswer, ToolError
from .fields import convert_value

ITEM_TYPE_NAMES = {"test": "Test", "testExecution": "Test Execution", "testPlan": "Test Plan",
                   "testSet": "Test Set", "precondition": "Precondition"}
SYSTEM_FIELDS = frozenset({"summary", "project", "issuetype", "reporter"})
MAX_CHOICES = 50
XRAY_CUSTOM_PREFIX = "com.xpandit.plugins.xray"
# create-meta schema types a flat elicitation form can hold (one primitive value)
_FORM_TYPES = frozenset({"string", "number", "date", "datetime", "option", "priority",
                         "version", "component", "securitylevel", "resolution"})
_NAME_TYPES = frozenset({"priority", "version", "component", "securitylevel", "resolution"})
_FORM_SPEC_TYPES = frozenset({"string", "number", "option", "version"})
# Shapes of system fields Jira may name as required, for converting answers without create-meta
_SYSTEM_SHAPES = {"components": ("array", "component"), "fixVersions": ("array", "version"),
                  "versions": ("array", "version"), "labels": ("array", "string"),
                  "priority": ("priority", None), "duedate": ("date", None),
                  "environment": ("string", None), "assignee": ("user", None)}
_SYSTEM_NAMES = {"components": "components", "component/s": "components",
                 "fix versions": "fixVersions", "fix version/s": "fixVersions",
                 "affects versions": "versions", "affects version/s": "versions",
                 "labels": "labels", "priority": "priority", "due date": "duedate",
                 "environment": "environment", "assignee": "assignee"}
# Jira's create error, as Xray returns it (recorded live): "Create issue failed! -
# components: Components is required., Components: Components is required." Each entry is
# "<field id or name>: <name> is required."; an entry may repeat the name as its key.
_REQUIRED_ENTRY = re.compile(r"(?:^|(?<=- |, ))((?:(?! - )[^:,])+?): ([^:,]+?) is required\.")


class RequiredFieldsRejected(ToolError):
    """Jira refused a create for missing required fields and nothing was created; the fields
    are learned for the server run, so the registry runs the call once more to ask for them."""


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


def parse_required_error(message: str) -> List[dict]:
    """Required fields named in a Jira create error -> create-meta-like entries
    {"id", "name", "required", "schemaType"[, "schemaItems"]}, one per field."""
    by_name: Dict[str, dict] = {}
    for key, name in _REQUIRED_ENTRY.findall(str(message or "")):
        key, name = key.strip(), name.strip()
        entry = by_name.get(name.casefold())
        if key != name or entry is None:  # an id beats the name repeated as the key
            by_name[name.casefold()] = {"id": key, "name": name}
    out = []
    for f in by_name.values():
        if f["id"] == f["name"]:  # only the name was given: a known system field's id
            f["id"] = _SYSTEM_NAMES.get(f["id"].casefold(), f["id"])
        shape = _SYSTEM_SHAPES.get(f["id"])
        entry = {"id": f["id"], "name": f["name"], "required": True,
                 "schemaType": shape[0] if shape else None, "allowedValues": []}
        if shape and shape[1]:
            entry["schemaItems"] = shape[1]
        out.append(entry)
    return out


def _learned(ctx: Any, pk: str, item_type: str) -> List[dict]:
    return list((getattr(ctx, "reported_fields", None) or {}).get((pk, item_type)) or [])


def learn(ctx: Any, pk: str, item_type: str, found: List[dict]) -> None:
    """Remember for the server run that Jira requires `found` on new items of the type, and
    drop the cached create-meta (it missed them or is stale)."""
    reported = getattr(ctx, "reported_fields", None)
    if reported is None:
        reported = ctx.reported_fields = {}
    known = {f["id"]: f for f in reported.get((pk, item_type)) or []}
    for f in found:
        known.setdefault(f["id"], f)
    reported[(pk, item_type)] = list(known.values())
    cache = getattr(ctx, "create_meta_cache", None)
    if cache:
        cache.pop((pk, item_type), None)


@contextmanager
def creating(ctx: Any, pk: str, item_type: str) -> Iterator[None]:
    """Wraps the create mutation of a new item: a Jira error naming required fields becomes
    RequiredFieldsRejected (the fields learned, see learn); other errors pass unchanged."""
    try:
        yield
    except NeedsAnswer:
        raise
    except ToolError as e:
        found = parse_required_error(e.message)
        if not found:
            raise
        learn(ctx, pk, item_type, found)
        tname = ITEM_TYPE_NAMES.get(item_type, item_type)
        raise RequiredFieldsRejected(
            "Jira did not create the new %s in %s: the required field(s) %s have no value. "
            "Nothing was created. Call again with `fields` set to {field id or name: value} "
            "(values as Jira expects them; see the field-ids guide)."
            % (tname, pk, ", ".join("%s (%s)" % (f["name"], f["id"]) for f in found)),
            projectKey=pk, itemType=item_type, jiraError=e.message,
            missingFields=[{"name": f["name"], "id": f["id"]} for f in found]) from None


def _discovered(meta: Optional[List[dict]], learned: List[dict]) -> Optional[List[dict]]:
    """create-meta plus the fields Jira reported as required (marked required)."""
    if not learned:
        return meta
    out = [dict(f) for f in meta or []]
    by_id = {f["id"]: f for f in out}
    for f in learned:
        if f["id"] in by_id:
            by_id[f["id"]]["required"] = True
        else:
            out.append(dict(f))
    return out


def _xray_owned(f: dict) -> bool:
    """A field Xray manages itself (Test Type, Pre-Condition Type, ...): never asked."""
    return str(f.get("schemaCustom") or "").startswith(XRAY_CUSTOM_PREFIX)


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
                 remember: bool, asked: bool = False) -> None:
    """Answers of this call: used by later resolutions of the call and remembered on commit.
    Asked answers are always remembered; call-supplied ones when `remember` or when nothing
    is remembered for the field yet."""
    values = {k: v for k, v in values.items() if k not in SYSTEM_FIELDS}
    if not values:
        return
    entry = _pending(ctx).setdefault((pk, item_type), {"values": {}, "store": {}})
    entry["values"].update(values)
    for k, v in values.items():
        entry["store"][k] = (v, asked or bool(remember))


def commit(ctx: Any) -> None:
    """Remember this call's answers (all validation passed; the write comes next). The
    answers stay usable for the rest of the call (e.g. new tests of a container)."""
    pending = _pending(ctx)
    site = getattr(ctx, "site", "")
    if not site:
        return
    from .choices import region_of
    region = region_of(ctx)
    for (pk, item_type), entry in pending.items():
        if not entry["store"]:
            continue
        known = ctx.preferences.field_values(region, site, pk, item_type)
        store = {k: v for k, (v, force) in entry["store"].items() if force or k not in known}
        ctx.preferences.remember_fields(region, site, pk, item_type, store)
        entry["store"] = {}


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
    create_meta = _meta(ctx, pk, item_type)
    meta = _discovered(create_meta, _learned(ctx, pk, item_type))
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
    remembered = {k: v for k, v in _remembered(ctx, pk, item_type).items()
                  if k not in SYSTEM_FIELDS}
    if create_meta is not None:  # a field no longer on the create screen would fail it
        ids = {f["id"] for f in meta or []} | set(by_id)
        remembered = {k: v for k, v in remembered.items() if k in ids}
    answers = (_pending(ctx).get((pk, item_type)) or {}).get("values") or {}
    out = dict(remembered)
    out.update(defaults)
    out.update(answers)
    out.update(call)
    # the tool's own values (e.g. description; summary, project, ...) always win
    own = set(provided) | SYSTEM_FIELDS
    out = {k: v for k, v in out.items() if k not in own}
    applied = {k: v for k, v in remembered.items()
               if k not in defaults and k not in answers and k not in call and k not in own}
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
        if not f.get("required") or fid in skip or _xray_owned(f):
            continue
        required.add(fid)
        if fid not in out and not f.get("hasDefaultValue") and \
                all(m["id"] != fid for m in missing):
            spec = by_id.get(fid)  # a settings spec (not required there) gives the name/type
            missing.append(_field(fid, f.get("name") or fid, spec.type if spec else
                                  f.get("schemaType"), f.get("allowedValues"), spec=spec,
                                  meta=f))
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
              param: str, unique_ids: bool = False) -> List[dict]:
    """needs_answer questions for missing required fields: name, id, type, first 50 allowed
    values. unique_ids (several item types in one question set): id is "<param>.<field id>"
    and `field` holds the field id."""
    tname = ITEM_TYPE_NAMES.get(item_type, item_type)
    out = []
    for f in missing:
        q: Dict[str, Any] = {
            "id": "%s.%s" % (param, f["id"]) if unique_ids else f["id"], "field": f["id"],
            "name": f["name"], "type": _type_label(f), "param": param,
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


def ask(ctx: Any, project: ProjectSettings, missing: Dict[str, dict],
        remember: bool = False) -> None:
    """Ask once for every missing field of every item type of the call: {itemType: {"param",
    "fields": {id: field}}}. One elicitation form when the client supports it and every
    field fits a flat form; otherwise one NeedsAnswer listing them all (each question names
    its `param`). The answers become this call's pending answers. Declined, cancelled or no
    answer: ToolError; nothing is written or remembered."""
    pk = project.key
    groups = [(t, m["param"], list(m["fields"].values())) for t, m in missing.items()
              if m["fields"]]
    if not groups:
        return
    every = [f for _, _, fs in groups for f in fs]
    multi = len(groups) > 1

    def label(t: str, f: dict) -> str:
        return "%s (%s)" % (f["name"], ITEM_TYPE_NAMES.get(t, t)) if multi else f["name"]

    names = ", ".join(label(t, f) for t, _, fs in groups for f in fs)
    what = " and ".join("new %s" % ITEM_TYPE_NAMES.get(t, t) for t, _, _ in groups)
    missing_data = [dict(d, itemType=t) if multi else d
                    for t, _, fs in groups for d in _missing_data(fs)]
    channel = getattr(ctx, "channel", None)
    if channel is not None and channel.supports_elicitation and all(map(_formable, every)):
        keys = {(t, f["id"]): ("%s.%s" % (t, f["id"]) if multi else f["id"])
                for t, _, fs in groups for f in fs}
        props: Dict[str, Any] = {}
        for t, _, fs in groups:
            form = _form(fs)
            for f in fs:
                prop = dict(form["properties"][f["id"]], title=label(t, f))
                props[keys[(t, f["id"])]] = prop
        content = channel.elicit(
            "The %s in Jira project %s need values for required fields: %s. The answers are "
            "remembered for your user as defaults for new items of the same type in %s."
            % (what, pk, names, pk),
            {"type": "object", "properties": props, "required": list(props)})
        answers: Dict[str, Dict[str, Any]] = {}
        for t, _, fs in groups:
            for f in fs:
                value = content.get(keys[(t, f["id"])]) if isinstance(content, dict) else None
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ToolError("No value was given for the required field(s) %s of the %s "
                                    "in %s; nothing was written and nothing was remembered."
                                    % (names, what, pk),
                                    projectKey=pk, missingFields=missing_data)
                answers.setdefault(t, {})[f["id"]] = _convert_answer(f, value)
        for t, values in answers.items():
            _add_pending(ctx, pk, t, values, remember, asked=True)
        return
    params = ", ".join("`%s` for the %s" % (p, ITEM_TYPE_NAMES.get(t, t))
                       for t, p, _ in groups)
    raise NeedsAnswer(
        "The %s in %s need values for required Jira fields: %s. Ask the user, then call "
        "again with %s set to {field id or name: value} (lists for multi-value fields, "
        "account ids for users); never answer it yourself. Nothing was written."
        % (what, pk, names, params),
        [q for t, p, fs in groups for q in questions(project, t, fs, p, multi)],
        projectKey=pk, missingFields=missing_data, **_untyped_hint(every))


def _untyped_hint(fields: List[dict]) -> Dict[str, str]:
    """Fields Jira named in an error, without create-meta: their type is unknown."""
    names = [f["name"] for f in fields if f["spec"] is None and not f["type"]]
    if not names:
        return {}
    return {"hint": "The type of %s is unknown without the Jira API token: pass each value "
                    "in the JSON shape Jira expects for it (for example {\"value\": ...} for "
                    "a select list, {\"name\": ...} for a component); see the field-ids "
                    "guide." % ", ".join(names)}


class Collector:
    """One pass over every resolution of a call: compute() each, collecting what is
    missing (per item type), the remembered values used and the call's answers."""

    def __init__(self, ctx: Any, project: ProjectSettings) -> None:
        self.ctx, self.project = ctx, project
        self.missing: Dict[str, dict] = {}
        self.applied: Dict[str, Dict[str, Any]] = {}
        self.answered: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, item_type: str, fields: Optional[Dict[str, Any]],
                 raw_fields: Optional[Dict[str, Any]] = None, param: str = "fields",
                 provided: Iterable[str] = (), record: bool = True,
                 required: bool = True) -> Dict[str, Any]:
        """Jira fields of one item. record=False: the call's values are not remembered
        (tag rule values); required=False: missing fields are not asked (the result is not
        used for a new item)."""
        r = compute(self.ctx, self.project, item_type, fields, raw_fields, provided)
        if required and r.missing:
            entry = self.missing.setdefault(item_type, {"param": param, "fields": {}})
            for f in r.missing:
                entry["fields"].setdefault(f["id"], f)
        if r.applied:
            self.applied.setdefault(item_type, {}).update(r.applied)
        if record and r.answered:
            self.answered.append((item_type, r.answered))
        return r.fields

    def questions(self) -> List[dict]:
        multi = len(self.missing) > 1
        return [q for t, m in self.missing.items()
                for q in questions(self.project, t, list(m["fields"].values()), m["param"],
                                   multi)]


def resolve_all(ctx: Any, project: ProjectSettings, build: Callable[[Collector], Any],
                remember: bool = False) -> Tuple[Any, Dict[str, Dict[str, Any]]]:
    """Run build(collector) (every resolution of the call); when fields are missing, ask
    once for all of them (see ask) and run it again. -> (build's result, applied defaults
    {itemType: {id: value}})."""
    c = Collector(ctx, project)
    result = build(c)
    if c.missing:
        ask(ctx, project, c.missing, remember)
        c = Collector(ctx, project)
        result = build(c)
        if c.missing:
            raise ToolError("Required fields are still missing after the answers; nothing "
                            "was written", missingFields=[
                                d for m in c.missing.values()
                                for d in _missing_data(list(m["fields"].values()))])
    for item_type, values in c.answered:
        _add_pending(ctx, project.key, item_type, values, remember)
    return result, c.applied


def resolve(ctx: Any, project: ProjectSettings, item_type: str,
            fields: Optional[Dict[str, Any]], raw_fields: Optional[Dict[str, Any]],
            remember: bool = False, *, param: str = "fields", provided: Iterable[str] = (),
            record: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """-> (Jira fields, applied remembered values) of one item (see resolve_all)."""
    out, applied = resolve_all(
        ctx, project, lambda c: c(item_type, fields, raw_fields, param, provided, record),
        remember)
    return out, applied.get(item_type, {})


def add_applied(out: Dict[str, Any], item_type: str, applied: Dict[str, Any]) -> None:
    """Report remembered values a create used: out["appliedDefaults"][itemType]."""
    if applied:
        out.setdefault("appliedDefaults", {}).setdefault(item_type, {}).update(applied)
