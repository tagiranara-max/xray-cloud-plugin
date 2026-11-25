from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .config import REGIONS, _setting, ai_created_problem, feature_import_problem
from .context import ServerContext
from .errors import ToolError

# name -> {question, kind, options?}. Only the first five are asked as blocking questions;
# unmatchedTags and tagRules resolve to (None, None) when unanswered and callers apply the
# defaults ("keep", []).
CHOICES: Dict[str, Dict[str, Any]] = {
    "createTestSet": {
        "question": "Create a Test Set that groups the tests imported from this feature file?",
        "kind": "bool", "options": [True, False]},
    "backgrounds": {
        "question": "How should Gherkin Background steps be imported: as Precondition issues "
                    "linked to the tests, or inlined into each test?",
        "kind": "enum", "options": ["preconditions", "inline"]},
    "tagsAsLabels": {
        "question": "Add the Gherkin tags as Jira labels on the created tests?",
        "kind": "bool", "options": [True, False]},
    "unmatchedTags": {
        "question": "What should happen to tags that no tag rule covers: keep them as labels "
                    "or skip them?",
        "kind": "enum", "options": ["keep", "skip"]},
    "aiLabel": {
        "question": "Which label (no whitespace) should mark newly created items as "
                    "AI-created? Answer with an empty value to not mark them.",
        "kind": "label"},
    "aiLabelScope": {
        "question": "Should the AI label go on tests only, or on every created item "
                    "(test sets, preconditions, executions, plans)?",
        "kind": "enum", "options": ["tests", "all"]},
    "tagRules": {
        "question": "Which tag rules should map Gherkin tags to labels or fields?",
        "kind": "rules"},
}

# choice name -> (settings section, key inside the section)
_SETTINGS_KEYS = {
    "createTestSet": ("feature_import", "createTestSet"),
    "backgrounds": ("feature_import", "backgrounds"),
    "tagsAsLabels": ("feature_import", "tagsAsLabels"),
    "unmatchedTags": ("feature_import", "unmatchedTags"),
    "tagRules": ("feature_import", "tagRules"),
    "aiLabel": ("ai_created", "label"),
    "aiLabelScope": ("ai_created", "scope"),
}


def region_of(ctx: ServerContext) -> str:
    region = (_setting(ctx.env, "XRAY_REGION") or "").lower()
    return region if region in REGIONS else "global"


def _from_settings(ctx: ServerContext, project_key: str, name: str) -> Tuple[bool, Any]:
    settings = ctx.settings_store.get()
    section, key = _SETTINGS_KEYS[name]
    project = settings.projects.get(project_key)
    for source in (getattr(project, section) if project else {}, getattr(settings, section)):
        if key in source:
            value = source[key]
            if name == "aiLabel" and value is None:
                value = ""  # null means "do not mark": an answer
            return True, value
    return False, None


def resolve(ctx: ServerContext, project_key: str, name: str,
            call_value: Any) -> Tuple[Any, Optional[str]]:
    """(value, source): call parameter, then settings (project, then top level), then the
    remembered preference; (None, None) when unanswered."""
    if call_value is not None:
        return call_value, "call"
    found, value = _from_settings(ctx, project_key, name)
    if found:
        return value, "settings"
    found, value = _remembered(ctx, project_key, name)
    return (value, "remembered") if found else (None, None)


def remembered_value(ctx: ServerContext, project_key: str, name: str) -> Any:
    """The remembered (checked) value of a choice, else None."""
    return _remembered(ctx, project_key, name)[1]


def _remembered(ctx: ServerContext, project_key: str, name: str) -> Tuple[bool, Any]:
    region = region_of(ctx)
    remembered = ctx.preferences.get(region, project_key, ctx.site)
    if name not in remembered:
        return False, None
    value = remembered[name]
    problem = _remembered_problem(name, value)
    if problem:
        path = ctx.preferences.path
        raise ToolError("The preferences file %s has an invalid remembered %s for %s/%s: "
                        "%s; fix or delete that entry, or set it with set_preferences"
                        % (path, name, region, project_key, problem),
                        file=str(path), key="%s/%s" % (region, project_key), choice=name)
    return True, value


def add_tag_rules(ctx: ServerContext, project_key: str, rules: list) -> None:
    """Append raw tag rules (answers to the import's tag questions) to the project's
    remembered tagRules."""
    if not rules:
        return
    current = remembered_value(ctx, project_key, "tagRules") or []
    ctx.preferences.update(region_of(ctx), project_key,
                           {"tagRules": list(current) + list(rules)}, ctx.site)


def _remembered_problem(name: str, value: Any) -> Optional[str]:
    """The settings-file checks, applied to a (possibly hand-edited) remembered value. The
    aiLabel follows the call parameter's rule: "" (don't mark) or a label without whitespace.
    Remembered tagRules contents are checked when parsed (gherkin.tagrules.parse_rules)."""
    if name == "aiLabel":
        if not isinstance(value, str) or any(c.isspace() for c in value):
            return 'must be "" (do not mark) or a label without whitespace'
        return None
    if name == "aiLabelScope":
        return ai_created_problem("scope", value)
    return feature_import_problem(name, value)


# Applied when unresolved (ruling R1): not blocking questions.
DEFAULTS: Dict[str, Any] = {"unmatchedTags": "keep", "tagRules": []}


def effective(ctx: ServerContext, project_key: str) -> Dict[str, Dict[str, Any]]:
    """name -> {"value", "source"} for every choice of the project, resolved without a call
    value. Unresolved: the default with source "default" for unmatchedTags/tagRules, else
    {"source": "unanswered"} (no value). Settings tagRules are parsed TagRule objects, shown
    as a count rather than serialised."""
    out: Dict[str, Dict[str, Any]] = {}
    for name in CHOICES:
        value, source = resolve(ctx, project_key, name, None)
        if source is None and name in DEFAULTS:
            value, source = DEFAULTS[name], "default"
        if source is None:
            out[name] = {"source": "unanswered"}
            continue
        if name == "tagRules" and value and not isinstance(value[0], dict):
            value = "%d rule(s) from the settings file" % len(value)
        out[name] = {"value": value, "source": source}
    return out


def remember(ctx: ServerContext, project_key: str, answers: Dict[str, Any], force: bool) -> None:
    """Store answers: when nothing is remembered for the name, or when the value differs and
    `force`. Identical values, None values and unknown names are ignored."""
    region = region_of(ctx)
    current = ctx.preferences.get(region, project_key, ctx.site)
    to_store = {n: v for n, v in answers.items()
                if n in CHOICES and v is not None
                and (n not in current or (current[n] != v and force))}
    if to_store:
        ctx.preferences.update(region, project_key, to_store, ctx.site)


def question(name: str, context: Dict[str, Any]) -> Dict[str, Any]:
    spec = CHOICES[name]
    out: Dict[str, Any] = {"id": name, "question": spec["question"]}
    if "options" in spec:
        out["options"] = list(spec["options"])
    out["context"] = context
    return out
