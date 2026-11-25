"""Tag-rule field values the plugin cannot convert itself (feature import).

A tag rule's `field` action sends the tag's value to a Jira field. Text, numbers and dates go
as they are; an option, priority, component or version is matched by name (case-insensitive)
against Jira's create metadata when the Jira token gives it, else shaped by the field's type.
Anything else (an Atlassian Team, a user picker, a cascading select, a field whose type is
unknown without the Jira token) is never guessed: the import asks once per (field, value) what
to send, as free text (the id or the exact value). The answer is remembered for the project
(per site, keyed by field and tag value) and consulted by every rule, settings or remembered;
a rule's own `values` map wins over it. Later imports with the same value do not ask again."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .config import ProjectSettings
from .errors import ToolError
from .gherkin.tagrules import FIELD_ID, SYSTEM_FIELD_IDS, mapped_value

PARAM = "tagValueAnswers"
PREFIX = "tagValue."
MAX_SCENARIOS = 5

PARAM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "object", "additionalProperties": {}},
    "description": "Only after tagValue.<field>.<value> questions: the user's answer per field "
                   "and tag value, {field: {tag value: id or exact value}} (a JSON object or "
                   "list in the shape Jira expects also works). Never answer it yourself; ask "
                   "the user. Remembered for the project."}

_TEXT = frozenset({"string", "number", "date", "datetime", "raw"})
_NAMED = frozenset({"priority", "component", "version", "securitylevel", "resolution"})
_SPEC_KIND = {"option": ("option", False), "options": ("option", True),
              "version": ("version", False), "versions": ("version", True)}


def _match(allowed: List[Any], value: str) -> Optional[str]:
    labels = [str(a) for a in allowed if a is not None]
    if value in labels:
        return value
    folded = [a for a in labels if a.casefold() == value.casefold()]
    return folded[0] if len(folded) == 1 else None


def _shaped(kind: str, many: bool, allowed: List[Any], value: Any,
            name: str) -> Tuple[Any, Optional[str]]:
    """An option / named value: matched against the allowed values when there are any."""
    if not isinstance(value, str) or not allowed:
        return value, None  # shaped by the field's type later
    m = _match(allowed, value)
    if m is None:
        return None, "%s has no %s named %r" % (name, "option" if kind == "option" else kind,
                                                value)
    one = {"value": m} if kind == "option" else {"name": m}
    return ([one] if many else one), None


def convert(ctx: Any, project: ProjectSettings, name: str, value: Any
            ) -> Tuple[Any, Optional[str]]:
    """(value to pass on, None), or (None, why the value is asked). A value passed on is
    shaped later by the field's type (requiredfields / fields); a matched option or named
    value is already shaped. A field id create metadata does not list is passed on unchanged
    (the create refuses it, an existing test gets a notDone)."""
    from .requiredfields import _SYSTEM_SHAPES, _lookup, _meta
    spec = (project.fields.get("test") or {}).get(name)
    meta = _meta(ctx, project.key, "test")
    if spec is not None:
        f = _lookup(meta, spec.id) if spec.id else None
        allowed = (f or {}).get("allowedValues") or []
        if spec.type in _SPEC_KIND:
            kind, many = _SPEC_KIND[spec.type]
            return _shaped(kind, many, allowed, value, name)
        if spec.type in _TEXT:
            return value, None
        return None, "%s is a %s field" % (name, spec.type)
    if name in SYSTEM_FIELD_IDS:
        t, items = _SYSTEM_SHAPES[name]
        kind = items if t == "array" else t
        f = _lookup(meta, name)
        if kind in _NAMED:
            return _shaped(kind, t == "array", (f or {}).get("allowedValues") or [], value, name)
        if kind == "user":
            return None, "%s is a user field (Jira needs an account id)" % name
        return value, None
    if not FIELD_ID.fullmatch(name):
        return value, None
    if meta is None:
        return None, "the type of %s is unknown without the Jira API token" % name
    f = _lookup(meta, name)
    if f is None:
        return value, None
    t, items = f.get("schemaType"), f.get("schemaItems")
    kind = items if t == "array" else t
    label = f.get("name") or name
    if kind in _TEXT:
        return value, None
    if kind == "option" or kind in _NAMED:
        return _shaped(kind, t == "array", f.get("allowedValues") or [], value, label)
    return None, "%s (%s) is a %s field" % (label, name, "%s of %s" % (t, items)
                                             if t == "array" else (t or "custom"))


def rule_fields(ctx: Any, project: ProjectSettings, outcome: Any
                ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(fields to pass on, {field name: why asked}) of one scenario's rule outcome. Values from
    a value map or an answer are passed on as given."""
    out: Dict[str, Any] = {}
    asked: Dict[str, str] = {}
    for name, value in outcome.fields.items():
        if name in outcome.mapped:
            out[name] = value
            continue
        conv, why = convert(ctx, project, name, value)
        if why:
            asked[name] = why
        else:
            out[name] = conv
    return out, asked


def qid(name: str, value: str) -> str:
    return "%s%s.%s" % (PREFIX, name, value)


def collect(ctx: Any, project: ProjectSettings, feature: Any, analysis: Any,
            has_jira: bool, overridden) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Open questions {(field, tag value): {field, value, why, scenarios}} over the scenarios
    whose rule fields may be written: new tests (minus the fields the call itself sets,
    `overridden(name)`), and existing tests when Jira is there to write them."""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for i, st in enumerate(analysis.scenarios):
        if st.blocked or (st.test_key and not has_jira):
            continue
        _, asked = rule_fields(ctx, project, st.outcome)
        for name, why in asked.items():
            if overridden(name):  # the call's value is used (new) or reported (existing)
                continue
            raw = st.outcome.raw.get(name, str(st.outcome.fields[name]))
            entry = out.setdefault((name, raw), {"field": name, "value": raw, "why": why,
                                                 "scenarios": []})
            s = feature.scenarios[i]
            entry["scenarios"].append("%s (line %d)" % (s.name, s.line))
    return out


def question(entry: Dict[str, Any]) -> Dict[str, Any]:
    name, value = entry["field"], entry["value"]
    scen = entry["scenarios"]
    return {
        "id": qid(name, value), "field": name, "value": value, "param": PARAM,
        "question": ("What is `%s` for %s? The plugin cannot convert it itself (%s). Give the "
                     "id or the exact value Jira expects for %s (for an Atlassian Team its "
                     "id, for a user the account id). The answer is remembered for this "
                     "project, so later imports with %r do not ask again."
                     % (value, name, entry["why"], name, value)),
        "type": "text",
        "context": {"field": name, "value": value, "reason": entry["why"],
                    "scenarios": scen[:MAX_SCENARIOS],
                    "moreScenarios": max(0, len(scen) - MAX_SCENARIOS)}}


def questions(pending: Dict[Tuple[str, str], Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [question(e) for e in pending.values()]


def _clean(value: Any, where: str) -> Any:
    if isinstance(value, (dict, list)) and value:
        return value
    if not isinstance(value, str) or not value.strip():
        raise ToolError("%s must be a non-empty string (the id or exact value), or a JSON "
                        "object or list in the shape Jira expects" % where)
    text = value.strip()
    if text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        if isinstance(parsed, (dict, list)) and parsed:
            return parsed
    return text


def parse(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Validate tagValueAnswers -> {field: {tag value: answer}}."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError("%s must be an object {field: {tag value: answer}}" % PARAM)
    out: Dict[str, Dict[str, Any]] = {}
    for name, values in raw.items():
        where = "%s.%s" % (PARAM, name)
        if not isinstance(values, dict) or not values:
            raise ToolError("%s must be an object {tag value: answer}" % where)
        out[str(name)] = {str(v): _clean(a, "%s.%s" % (where, v)) for v, a in values.items()}
    return out


def answer_for(answers: Dict[str, Dict[str, Any]], name: str, value: str
               ) -> Tuple[bool, Any]:
    return mapped_value(answers.get(name) or {}, value)


def unanswered(pending: Dict[Tuple[str, str], Dict[str, Any]],
               answers: Dict[str, Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    return {k: e for k, e in pending.items() if not answer_for(answers, *k)[0]}


def form(pending: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
    return {qid(n, v): {"type": "string", "title": "What is `%s` for %s?" % (v, n),
                        "description": "%s. The id or the exact value Jira expects; "
                                       "remembered for the project." % e["why"]}
            for (n, v), e in pending.items()}


def from_form(content: Dict[str, Any], pending: Dict[Tuple[str, str], Dict[str, Any]]
              ) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for (n, v) in pending:
        a = content.get(qid(n, v)) if isinstance(content, dict) else None
        if not isinstance(a, str) or not a.strip():
            raise ToolError("No value was given for `%s` of %s; nothing was written and "
                            "nothing was remembered." % (v, n), field=n, value=v)
        out.setdefault(n, {})[v] = _clean(a, qid(n, v))
    return out


def used_answers(analysis: Any, answers: Dict[str, Dict[str, Any]]
                 ) -> Dict[str, Dict[str, Any]]:
    """The answers (call or form) the import used: {field: {tag value: answer}}, to remember
    for the project (choices.add_tag_values)."""
    out: Dict[str, Dict[str, Any]] = {}
    for st in analysis.scenarios:
        o = st.outcome
        for name, how in o.mapped.items():
            if how != "answer":
                continue
            raw = o.raw.get(name, "")
            found, ans = answer_for(answers, name, raw)
            if found:
                out.setdefault(name, {})[raw] = ans
    return out
