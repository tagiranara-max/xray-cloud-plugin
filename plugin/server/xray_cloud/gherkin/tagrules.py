"""Tag rules: map Gherkin tags to labels, Test Sets and fields.

Rules are plain data from the settings file or remembered preferences. No rule is built in.
Tags arrive without "@", and issue-key tags are removed by the caller beforehand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Set, Tuple

from ..errors import ToolError

RULE_KEYS = ("tag", "match", "split", "label", "testSet", "field", "values", "skip")
_PLACEHOLDER = re.compile(r"\{(value|key|tag)\}|\$([1-9])")
_BRACES = re.compile(r"\{([^{}]*)\}")
_SEPARATED = re.compile(r"([^=:]+)[=:](.*)", re.DOTALL)
_WHITESPACE = re.compile(r"\s")
# a Jira custom field id: accepted in a rule's `field` without a settings entry
FIELD_ID = re.compile(r"customfield_\d+")
# Jira system field ids a rule's `field` may name (the shapes are in
# requiredfields._SYSTEM_SHAPES; a unit test keeps both in step)
SYSTEM_FIELD_IDS = frozenset({"components", "fixVersions", "versions", "labels", "priority",
                              "duedate", "environment", "assignee", "reporter"})


def is_field_id(name: str) -> bool:
    """A customfield_<n> id or a system field id: accepted without a settings entry."""
    return bool(FIELD_ID.fullmatch(str(name))) or name in SYSTEM_FIELD_IDS


def mapped_value(values: Dict[str, Any], value: str) -> Tuple[bool, Any]:
    """(found, mapped) for a value in a value map: exact key first, then case-insensitive
    (only when one key matches)."""
    if value in values:
        return True, values[value]
    folded = [k for k in values if k.casefold() == value.casefold()]
    if len(folded) == 1:
        return True, values[folded[0]]
    return False, None


def kv_key(tag: str) -> Optional[str]:
    """The lower-cased key of a `key:value` or `key=value` tag (non-empty value), else None."""
    m = _SEPARATED.fullmatch(tag)
    if m is None or not m.group(1).strip() or not m.group(2).strip():
        return None
    return m.group(1).strip().lower()


class TagRuleError(ToolError):
    """A tag rule is malformed. Settings loading re-raises it as a SettingsError."""


@dataclass
class TagRule:
    keys: Optional[List[str]]          # lower-cased keys (key form), else None
    regex: Optional[Pattern[str]]      # regex form, else None
    split: Optional[str] = None
    labels: List[str] = field(default_factory=list)       # label templates
    test_sets: List[str] = field(default_factory=list)    # Test Set name templates
    fields: Dict[str, str] = field(default_factory=dict)  # friendly field name -> template
    skip: bool = False
    # tag value (rendered field template) -> value sent to Jira (an id, or the exact value)
    values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TagOutcome:
    labels: List[str] = field(default_factory=list)
    test_sets: List[str] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)
    consumed: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    sources: Dict[str, str] = field(default_factory=dict)  # field name -> "tag (rule n)"
    raw: Dict[str, str] = field(default_factory=dict)      # field name -> rendered tag value
    rules: Dict[str, int] = field(default_factory=dict)    # field name -> rule index
    # field name -> "rule" (the rule's value map gave the value) or "answer" (the call's
    # tagValueAnswers did); such values are sent as given (shaped by the field's type)
    mapped: Dict[str, str] = field(default_factory=dict)


def _templates(raw: Any, where: str) -> List[str]:
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not items or not all(isinstance(t, str) for t in items):
        raise TagRuleError("%s must be a string or a non-empty list of strings" % where)
    return list(items)


def _check_template(tpl: str, regex: Optional[Pattern[str]], where: str) -> None:
    for name in _BRACES.findall(tpl):
        if name not in ("value", "key", "tag"):
            raise TagRuleError("%s: unknown placeholder {%s}; use {value}, {key} or {tag}"
                               % (where, name))
        if regex is not None and name in ("value", "key"):
            raise TagRuleError("%s: {%s} is only available in the key form (`tag`); use "
                               "{tag} or $1..$9 with `match`" % (where, name))
    for n in re.findall(r"\$([1-9])", tpl):
        if regex is None:
            raise TagRuleError("%s: $%s is only available in the regex form (`match`)"
                               % (where, n))
        if int(n) > regex.groups:
            raise TagRuleError("%s: $%s refers to a group the regex does not have (it has %d)"
                               % (where, n, regex.groups))


def _parse_rule(raw: Any, field_names: Set[str], where: str) -> TagRule:
    if not isinstance(raw, dict):
        raise TagRuleError("%s must be a mapping" % where)
    for k in raw:
        if k not in RULE_KEYS:
            raise TagRuleError("%s: unknown key %r; expected one of: %s"
                               % (where, k, ", ".join(RULE_KEYS)))
    if ("tag" in raw) == ("match" in raw):
        raise TagRuleError("%s needs exactly one of `tag` (key form) or `match` (regex form)"
                           % where)
    keys: Optional[List[str]] = None
    regex: Optional[Pattern[str]] = None
    if "tag" in raw:
        ks = _templates(raw["tag"], where + ".tag")
        if any(not k.strip() or _SEPARATED.fullmatch(k) for k in ks):
            raise TagRuleError("%s.tag: keys must be non-empty and contain no = or :" % where)
        keys = [k.strip().lower() for k in ks]
    else:
        pattern = raw["match"]
        if not isinstance(pattern, str) or not pattern:
            raise TagRuleError("%s.match must be a non-empty regular expression string" % where)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise TagRuleError("%s.match is not a valid regular expression: %s" % (where, exc))
    split = raw.get("split")
    if "split" in raw:
        if regex is not None:
            raise TagRuleError("%s: `split` only applies to the key form (`tag`)" % where)
        if not isinstance(split, str) or not split:
            raise TagRuleError("%s.split must be a non-empty string" % where)
    skip = raw.get("skip", False)
    if "skip" in raw and not isinstance(skip, bool):
        raise TagRuleError("%s.skip must be true or false" % where)
    labels = _templates(raw["label"], where + ".label") if "label" in raw else []
    test_sets = _templates(raw["testSet"], where + ".testSet") if "testSet" in raw else []
    fields: Dict[str, str] = {}
    if "field" in raw:
        f = raw["field"]
        if not isinstance(f, dict) or not f:
            raise TagRuleError("%s.field must be a non-empty mapping of field name to template"
                               % where)
        for name, tpl in f.items():
            if not isinstance(tpl, str):
                raise TagRuleError("%s.field.%s must be a template string" % (where, name))
            if name not in field_names and not is_field_id(str(name)):
                known = ", ".join(sorted(field_names)) or "none defined"
                raise TagRuleError("%s.field: unknown field name %r (fields.test has: %s; a "
                                   "customfield_<n> id or a system field id such as priority, "
                                   "components or fixVersions also works)"
                                   % (where, name, known))
            fields[str(name)] = tpl
    values: Dict[str, Any] = {}
    if "values" in raw:
        v = raw["values"]
        if not fields:
            raise TagRuleError("%s.values only goes with `field`" % where)
        if not isinstance(v, dict) or not all(isinstance(k, str) and k.strip() for k in v):
            raise TagRuleError("%s.values must be a mapping of tag value to the value Jira "
                               "expects (an id or the exact value)" % where)
        for k, val in v.items():
            if not isinstance(val, (str, dict, list)) or (isinstance(val, str)
                                                          and not val.strip()):
                raise TagRuleError("%s.values.%s must be a non-empty string (or a JSON object "
                                   "or list in the shape Jira expects)" % (where, k))
        values = dict(v)
    has_action = bool(labels or test_sets or fields)
    if skip and has_action:
        raise TagRuleError("%s: `skip: true` cannot be combined with label, testSet or field"
                           % where)
    if not skip and not has_action:
        raise TagRuleError("%s has no action; add label, testSet, field or skip: true" % where)
    for tpl in labels + test_sets + list(fields.values()):
        _check_template(tpl, regex, where)
    return TagRule(keys, regex, split, labels, test_sets, fields, skip, values)


def parse_rules(raw: List[Any], field_names: Set[str], where: str) -> List[TagRule]:
    """Parse and validate rules. `where` names the list (e.g. `featureImport.tagRules`)."""
    if not isinstance(raw, list):
        raise TagRuleError("%s must be a list" % where)
    return [_parse_rule(r, field_names, "%s[%d]" % (where, i)) for i, r in enumerate(raw)]


def _render(tpl: str, value: str, key: str, tag: str, groups: Optional[tuple]) -> str:
    def sub(m: "re.Match[str]") -> str:
        if m.group(1):
            return {"value": value, "key": key, "tag": tag}[m.group(1)]
        g = groups[int(m.group(2)) - 1] if groups else None
        return g or ""
    return _PLACEHOLDER.sub(sub, tpl)


def _match(rule: TagRule, tag: str):
    """(key, values, groups) when the rule matches the tag, else None."""
    if rule.regex is not None:
        m = rule.regex.fullmatch(tag)
        return None if m is None else ("", [tag], m.groups())
    m = _SEPARATED.fullmatch(tag)
    if m is None or m.group(1).strip().lower() not in (rule.keys or []):
        return None
    value = m.group(2)
    values = [v.strip() for v in value.split(rule.split)] if rule.split else [value]
    values = [v for v in values if v.strip()]
    return (m.group(1), values, ()) if values else None


def _add(items: List[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _check_label(label: str) -> bool:
    return bool(label) and not _WHITESPACE.search(label)


def apply_rules(tags: List[str], rules: List[TagRule], tags_as_labels: bool,
                unmatched: str = "keep") -> TagOutcome:
    """tags_as_labels only governs plain tags no rule covers; a rule's `label` action always
    applies. An uncovered `key:value` tag is never a label (the import asks about its key)."""
    out = TagOutcome()
    for tag in tags:
        hit = None
        for index, rule in enumerate(rules):
            m = _match(rule, tag)
            if m is not None:
                hit = (index, rule, m)
                break
        if hit is None:
            out.unmatched.append(tag)
            if unmatched == "keep" and tags_as_labels and kv_key(tag) is None:
                if _check_label(tag):
                    _add(out.labels, tag)
                else:
                    out.problems.append("tag %r (kept as a label): invalid label %r; labels "
                                        "must be non-empty and contain no whitespace"
                                        % (tag, tag))
            continue
        index, rule, (key, values, groups) = hit
        out.consumed.append(tag)
        if rule.skip:
            continue
        who = "tag %r (rule %d)" % (tag, index)
        for value in values:
            def r(t: str) -> str:
                return _render(t, value, key, tag, groups)
            for t in rule.labels:
                label = r(t)
                if _check_label(label):
                    _add(out.labels, label)
                else:
                    out.problems.append("%s: label result %r is invalid; labels must be "
                                        "non-empty and contain no whitespace" % (who, label))
            for t in rule.test_sets:
                name = r(t).strip()
                if name:
                    _add(out.test_sets, name)
                else:
                    out.problems.append("%s: Test Set name result %r is empty" % (who, r(t)))
            for fname, t in rule.fields.items():
                raw = r(t)
                found, value_ = mapped_value(rule.values, raw)
                _set_field(out, fname, value_ if found else raw, who, raw, index,
                           "rule" if found else None)
    return out


def _set_field(out: TagOutcome, name: str, value: Any, who: str, raw: Optional[str] = None,
               index: Optional[int] = None, mapped: Optional[str] = None) -> None:
    if name in out.fields and out.fields[name] != value:
        out.problems.append("field %r gets conflicting values %r (%s) and %r (%s)"
                            % (name, out.fields[name], out.sources.get(name, "?"), value, who))
    elif name not in out.fields:
        out.fields[name] = value
        out.sources[name] = who
        out.raw[name] = raw if raw is not None else str(value)
        if index is not None:
            out.rules[name] = index
        if mapped:
            out.mapped[name] = mapped


def merge_outcomes(*outcomes: TagOutcome) -> TagOutcome:
    """Combine feature, rule and scenario outcomes. Conflicting field values become problems."""
    merged = TagOutcome()
    for o in outcomes:
        for label in o.labels:
            _add(merged.labels, label)
        for name in o.test_sets:
            _add(merged.test_sets, name)
        for tag in o.consumed:
            _add(merged.consumed, tag)
        for tag in o.unmatched:
            _add(merged.unmatched, tag)
        for p in o.problems:
            _add(merged.problems, p)
        for name, value in o.fields.items():
            _set_field(merged, name, value, o.sources.get(name, "?"), o.raw.get(name),
                       o.rules.get(name), o.mapped.get(name))
    return merged
