"""Questions about key:value tags no tag rule covers (feature import).

A `@key:value` or `@key=value` tag whose key no settings or remembered rule covers is not
turned into anything silently: the import asks, once per key, what the key means. Each answer
becomes a tag rule remembered for the project, so later tags with the same key follow it."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .config import ProjectSettings
from .errors import NeedsAnswer, ToolError
from .gherkin.tagrules import FIELD_ID

PARAM = "tagRuleAnswers"
ACTIONS = ("label_as_is", "label_value", "test_set", "field", "skip")
_EXTRA = {"testSet": "test_set", "field": "field"}
MAX_EXAMPLES = 5

PARAM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "object", "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "testSet": {"type": "string"}, "field": {"type": "string"}}},
    "description": "Only after a tagRule.<key> question: the user's answer per tag key, "
                   "{key: {action, testSet?, field?}}. Never answer it yourself; ask the user. "
                   "Remembered as a tag rule of the project."}


def _written_key(tag: str) -> str:
    for i, c in enumerate(tag):
        if c in ":=":
            return tag[:i].strip()
    return tag


def suggested_test_set(tags: List[str]) -> str:
    """"<Key> {value}" with the key as first written, capitalised: "Suite {value}"."""
    key = _written_key(tags[0]) if tags else "Tag"
    return key[:1].upper() + key[1:] + " {value}"


def _example(tags: List[str]) -> str:
    return "@" + tags[0] if tags else "@key:value"


def question(key: str, tags: List[str], project: ProjectSettings) -> Dict[str, Any]:
    tag = tags[0] if tags else key + ":value"
    sep = next((c for c in tag if c in ":="), ":")
    value = tag.split(sep, 1)[1].strip()
    return {
        "id": "tagRule.%s" % key, "key": key, "param": PARAM,
        "question": ("The tag key %r (e.g. %s) has no tag rule. What should tags with this key "
                     "become? label_as_is = label %r; label_value = label %r; test_set = a "
                     "Test Set named by a template (default %r); field = a Jira field set to "
                     "the value (a fields.test name of the settings file, or a customfield_<n> "
                     "id: without the Jira token such an id is sent as text, so prefer a "
                     "fields.test name for option, user or list fields); skip = ignore the "
                     "tag. The answer is remembered for this project."
                     % (key, _example(tags), tag, value, suggested_test_set(tags))),
        "choices": list(ACTIONS),
        "context": {"key": key, "examples": ["@" + t for t in tags[:MAX_EXAMPLES]],
                    "moreExamples": max(0, len(tags) - MAX_EXAMPLES),
                    "suggestedTestSet": suggested_test_set(tags),
                    "fieldNames": sorted(project.fields.get("test") or {})}}


def questions(uncovered: Dict[str, List[str]], project: ProjectSettings) -> List[dict]:
    return [question(k, tags, project) for k, tags in uncovered.items()]


def parse_answers(raw: Any) -> Dict[str, Dict[str, str]]:
    """Validate the tagRuleAnswers parameter -> {lower-cased key: answer}."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError("%s must be an object {tag key: {action, testSet?, field?}}" % PARAM)
    out: Dict[str, Dict[str, str]] = {}
    for key, ans in raw.items():
        where = "%s.%s" % (PARAM, key)
        k = str(key).strip().lower()
        if not k or any(c in k for c in ":=") or any(c.isspace() for c in k):
            raise ToolError("%s: a tag key must be non-empty, without whitespace, : or =" % where)
        if not isinstance(ans, dict):
            raise ToolError("%s must be an object {action, testSet?, field?}" % where)
        if k in out:
            raise ToolError("%s: the key %r is answered twice (keys are case-insensitive)"
                            % (where, k))
        unknown = sorted(set(ans) - {"action", "testSet", "field"})
        if unknown:
            raise ToolError("%s: unknown key(s) %s; expected action, testSet, field"
                            % (where, ", ".join(unknown)))
        action = ans.get("action")
        if action not in ACTIONS:
            raise ToolError("%s.action must be one of: %s" % (where, ", ".join(ACTIONS)))
        clean: Dict[str, str] = {"action": action}
        for extra, needs in _EXTRA.items():
            v = ans.get(extra)
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            if not isinstance(v, str):
                raise ToolError("%s.%s must be a string" % (where, extra))
            if action != needs:
                raise ToolError("%s.%s only goes with action %s" % (where, extra, needs))
            clean[extra] = v.strip()
        if action == "field" and "field" not in clean:
            raise ToolError("%s: action field needs `field` (a fields.test name of the settings "
                            "file or a customfield_<n> id)" % where)
        out[k] = clean
    return out


def _field_name(value: str, project: ProjectSettings, where: str) -> str:
    specs = project.fields.get("test") or {}
    if value in specs:
        return value
    for name, spec in specs.items():
        if spec.id == value:
            return name
    if FIELD_ID.fullmatch(value):
        return value
    known = ", ".join(sorted(specs)) or "none defined"
    raise ToolError("%s.field: unknown field %r; use a fields.test name of the settings file "
                    "(%s) or a customfield_<n> id; nothing was written" % (where, value, known),
                    field=value)


def to_rule(key: str, answer: Dict[str, str], tags: List[str],
            project: ProjectSettings) -> Dict[str, Any]:
    """The raw tag rule (remembered form) for one answered key."""
    action = answer["action"]
    rule: Dict[str, Any] = {"tag": key}
    if action == "label_as_is":
        rule["label"] = "{tag}"
    elif action == "label_value":
        rule["label"] = "{value}"
    elif action == "test_set":
        rule["testSet"] = answer.get("testSet") or suggested_test_set(tags)
    elif action == "field":
        rule["field"] = {_field_name(answer["field"], project, "%s.%s" % (PARAM, key)):
                         "{value}"}
    else:
        rule["skip"] = True
    return rule


def _form(uncovered: Dict[str, List[str]]) -> dict:
    props: Dict[str, Any] = {}
    for key, tags in uncovered.items():
        props["tagRule.%s" % key] = {
            "type": "string", "enum": list(ACTIONS), "title": "Tags %s" % _example(tags),
            "description": "label_as_is = label as written; label_value = label with only "
                           "the value; test_set = Test Set; field = Jira field; skip = ignore"}
        props["tagRule.%s.testSet" % key] = {
            "type": "string", "title": "Test Set name for %s (test_set only)" % _example(tags),
            "description": "Name template; {value} is the tag's value. Empty: %s"
                           % suggested_test_set(tags)}
        props["tagRule.%s.field" % key] = {
            "type": "string", "title": "Jira field for %s (field only)" % _example(tags),
            "description": "A fields.test name of the settings file, or a customfield_<n> "
                           "id (sent as text without the Jira token; prefer a fields.test "
                           "name for option, user or list fields)"}
    return {"type": "object", "properties": props,
            "required": ["tagRule.%s" % k for k in uncovered]}


def ask(ctx: Any, uncovered: Dict[str, List[str]], project: ProjectSettings,
        changes: Optional[List[Dict[str, Any]]] = None) -> Tuple[Dict[str, Dict[str, str]],
                                                                  Optional[Dict[str, Any]]]:
    """Ask the user about every uncovered key, and (changes) about existing tests the import
    would change, at once: one elicitation form when the client supports it (answers
    returned, validated: (tag answers, existing-test answers or None)), else NeedsAnswer.
    Declined or incomplete: ToolError; nothing is written or remembered."""
    from . import existingtests
    changes = changes or []
    keys = ", ".join("@%s" % t[0] for t in uncovered.values() if t)
    parts = []
    if uncovered:
        parts.append("The feature file has key:value tags no tag rule covers (%s)." % keys)
    if changes:
        parts.append("Scenarios name existing tests the import would change (%s)."
                     % ", ".join(c["key"] for c in changes))
    channel = getattr(ctx, "channel", None)
    if channel is not None and channel.supports_elicitation:
        schema = _form(uncovered)
        if changes:
            schema["properties"].update(existingtests.form(changes))
        content = channel.elicit(
            " ".join(parts) + (" Choose what each key means; the answers are remembered as "
                               "tag rules of project %s." % project.key if uncovered else "")
            + (" Choose whether to update those tests, keep them unchanged or skip their "
               "scenarios (not remembered)." if changes else ""), schema)
        if not isinstance(content, dict):
            raise ToolError("The %s questions were not answered; nothing was written and "
                            "nothing was remembered." % ("tag" if not changes else "import"), projectKey=project.key,
                            tagKeys=list(uncovered))
        raw: Dict[str, Dict[str, Any]] = {}
        for key in uncovered:
            action = content.get("tagRule.%s" % key)
            if action not in ACTIONS:
                raise ToolError("No valid answer for the tag key %r; nothing was written and "
                                "nothing was remembered." % key, projectKey=project.key,
                                tagKeys=list(uncovered))
            ans: Dict[str, Any] = {"action": action}
            for extra, needs in _EXTRA.items():
                if action == needs:
                    ans[extra] = content.get("tagRule.%s.%s" % (key, extra))
            field = ans.get("field")
            if action == "field" and (not isinstance(field, str) or not field.strip()):
                raise ToolError("Tags %s: \"field\" was chosen but no Jira field was named in "
                                "the form; nothing was written and nothing was remembered."
                                % _example(uncovered[key]), projectKey=project.key,
                                tagKeys=[key])
            raw[key] = ans
        return parse_answers(raw), (existingtests.from_form(content, changes) if changes
                                    else None)
    asks = []
    if uncovered:
        asks.append("what each tag key means (%s set to {key: {action, testSet?, field?}})"
                    % PARAM)
    if changes:
        asks.append("whether to update, keep or skip the existing tests (%s, optionally %s)"
                    % (existingtests.PARAM, existingtests.OVERRIDES))
    raise NeedsAnswer(
        "%s Ask the user %s, then call again with the answers; never answer them yourself. "
        "Nothing was written." % (" ".join(parts), " and ".join(asks)),
        questions(uncovered, project) + ([existingtests.question(changes)] if changes else []),
        projectKey=project.key)


def pending(answers: Dict[str, Dict[str, str]], uncovered: Dict[str, List[str]],
            project: ProjectSettings) -> List[Dict[str, Any]]:
    """Raw rules for the answered keys that are still uncovered (other answers are unused)."""
    return [to_rule(k, answers[k], tags, project) for k, tags in uncovered.items()
            if k in answers]

