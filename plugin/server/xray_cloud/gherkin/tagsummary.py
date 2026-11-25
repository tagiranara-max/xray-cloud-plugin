"""Summarise the tags of parsed feature files so the agent can propose tag rules (spec 6.4a).
Pure: no calls. Issue-key tags are listed apart and never subject to rules (spec 6.4)."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..errors import ToolError
from .parse import ParsedFeature
from .plan import classify_tag
from .tagrules import _SEPARATED, TagRule, apply_rules

LEVELS = ("feature", "rule", "scenario")


def _bare(tag: str) -> str:
    return tag[1:] if tag.startswith("@") else tag


def _is_issue_key(tag: str, plain_keys: Set[str] = frozenset()) -> bool:
    """plain_keys: key-shaped tags the import looked up and found to be no issue (ordinary
    tags)."""
    if tag in plain_keys:
        return False
    try:
        return classify_tag(tag)[0] != "label"
    except ToolError:
        return True  # a malformed key tag is still meant as one; it is never a rule tag


def _occurrences(feature: ParsedFeature) -> List[Tuple[str, str, int]]:
    """(tag without "@", level, scenario index or -1) once per place it is written; a Rule's
    tags are counted once however many scenarios the Rule has."""
    out: List[Tuple[str, str, int]] = [(_bare(t), "feature", -1) for t in feature.tags]
    seen_rules: Set[Tuple[Any, ...]] = set()
    for i, s in enumerate(feature.scenarios):
        ident = (s.rule, tuple(s.rule_tags))
        if s.rule_tags and ident not in seen_rules:
            seen_rules.add(ident)
            out.extend((_bare(t), "rule", i) for t in s.rule_tags)
        out.extend((_bare(t), "scenario", i) for t in s.tags)
    return out


def _effective(feature: ParsedFeature, i: int) -> List[str]:
    s = feature.scenarios[i]
    return [_bare(t) for t in feature.tags + s.rule_tags + s.tags]


def _split(value: str) -> Tuple[List[str], bool]:
    if "," in value:
        return [v.strip() for v in value.split(",") if v.strip()], True
    return ([value.strip()] if value.strip() else []), False


def tag_values(features: Iterable[ParsedFeature]) -> List[str]:
    """Distinct values of key/value tags (comma lists split), in first-seen order: the
    candidates for existing Test Set names."""
    out: List[str] = []
    for f in features:
        for tag, _, _ in _occurrences(f):
            m = None if _is_issue_key(tag) else _SEPARATED.fullmatch(tag)
            if m:
                for v in _split(m.group(2))[0]:
                    if v not in out:
                        out.append(v)
    return out


def _where() -> Dict[str, int]:
    return {level: 0 for level in LEVELS}


def summarize_tags(features: List[ParsedFeature], rules: List[TagRule],
                   existing_set_names: Set[str],
                   key_tags: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """key_tags: the import's issue-key tag classifications ({tag, meaning, note?}); tags
    whose meaning is `plain` are then counted as plain tags, and keyTags lists them all."""
    plain_keys = {k["tag"] for k in key_tags or [] if k["meaning"] == "plain"}
    key_value: Dict[str, Dict[str, Any]] = {}
    plain: Dict[str, Dict[str, Any]] = {}
    issue_keys: List[str] = []
    distinct: List[str] = []
    key_scenarios: Dict[str, int] = {}
    for f in features:
        scen_keys: List[Set[str]] = [set() for _ in f.scenarios]
        for tag, level, _ in _occurrences(f):
            if _is_issue_key(tag, plain_keys):
                if tag not in issue_keys:
                    issue_keys.append(tag)
                continue
            if tag not in distinct:
                distinct.append(tag)
            m = _SEPARATED.fullmatch(tag)
            if m is None:
                p = plain.setdefault(tag, {"count": 0, "where": _where()})
                p["count"] += 1
                p["where"][level] += 1
                continue
            key = m.group(1).strip().lower()
            e = key_value.setdefault(key, {"values": {}, "separators": [], "commaLists": False,
                                           "where": _where(), "scenarios": 0})
            sep = tag[len(m.group(1))]
            if sep not in e["separators"]:
                e["separators"].append(sep)
            values, is_list = _split(m.group(2))
            e["commaLists"] = e["commaLists"] or is_list
            for v in values:
                e["values"][v] = e["values"].get(v, 0) + 1
            e["where"][level] += 1
        for i in range(len(f.scenarios)):
            for tag in _effective(f, i):
                m = None if _is_issue_key(tag, plain_keys) else _SEPARATED.fullmatch(tag)
                if m:
                    scen_keys[i].add(m.group(1).strip().lower())
        for keys in scen_keys:
            for k in keys:
                key_scenarios[k] = key_scenarios.get(k, 0) + 1
    for k, e in key_value.items():
        e["scenarios"] = key_scenarios.get(k, 0)
    outcome = apply_rules(distinct, rules, False, "keep")
    matching: Dict[str, List[str]] = {}
    for k, e in key_value.items():
        hits = [v for v in e["values"] if v in existing_set_names]
        if hits:
            matching[k] = hits
    out = {"keyValue": key_value, "plain": plain, "issueKeys": issue_keys,
           "handledByRules": list(outcome.consumed), "unmatched": list(outcome.unmatched),
           "valuesMatchingTestSets": matching}
    if key_tags is not None:
        out["keyTags"] = [dict(k) for k in key_tags]
    return out
