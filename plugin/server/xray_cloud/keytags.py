"""The feature import's question about issue-key tags Xray cannot place (feature import).

Xray tells tests (getTests) and the requirements it covers (getCoverableIssues) apart; for any
other key-shaped tag (`@ISO-8601`, a bug or task key, a key of no issue at all) its JQL simply
returns nothing, so the import cannot tell what the key is. Such a tag is never turned
into anything silently: the import asks, once per key, whether to link it (as a related /
requirement issue), keep it as a label, or ignore it. Nothing is remembered."""
from __future__ import annotations

from typing import Any, Dict, List

from .context import ISSUE_KEY_RE
from .errors import ToolError
from .gherkin.plan import KEY_ANSWERS

PARAM = "keyTagAnswers"

PARAM_SCHEMA: Dict[str, Any] = {
    "type": "object", "additionalProperties": {"type": "string", "enum": list(KEY_ANSWERS)},
    "description": "Only after keyTag.<KEY> questions: the user's answer per issue key, "
                   "{KEY: link | label | ignore}. Never answer it yourself; ask the user. Not "
                   "remembered."}


def parse(raw: Any) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError("%s must be an object {issue key: link | label | ignore}" % PARAM)
    out: Dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip().lstrip("@")
        if not ISSUE_KEY_RE.fullmatch(key):
            raise ToolError("%s: %r is not an issue key (e.g. PROJ-12)" % (PARAM, k))
        if v not in KEY_ANSWERS:
            raise ToolError("%s.%s must be one of: %s" % (PARAM, key, ", ".join(KEY_ANSWERS)))
        out[key] = v
    return out


def _where(places: List[Dict[str, str]]) -> str:
    return ", ".join("%s %r" % (p["level"], p["name"]) if p["name"] else p["level"]
                     for p in places)


def question(key: str, places: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "id": "keyTag.%s" % key, "key": key, "param": PARAM,
        "question": ("The tag @%s (on %s) looks like an issue key, but Xray doesn't know %s "
                     "as a test or a requirement (it may be another kind of issue, or not an "
                     "issue key at all). link = link the tests to %s as a related/requirement "
                     "issue (if Jira refuses the link, the test is created without it and the "
                     "link is listed in notDone); label = keep @%s as a label; ignore = drop "
                     "the tag. Not remembered." % (key, _where(places), key, key, key)),
        "choices": list(KEY_ANSWERS),
        "context": {"key": key, "where": [dict(p) for p in places]}}


def questions(asked: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, Any]]:
    return [question(k, places) for k, places in asked.items()]


def form(asked: Dict[str, List[Dict[str, str]]]) -> Dict[str, Any]:
    return {"keyTag.%s" % k: {
        "type": "string", "enum": list(KEY_ANSWERS), "title": "Tag @%s (%s)" % (k, _where(p)),
        "description": "Xray doesn't know %s as a test or a requirement. link = link as a "
                       "related/requirement issue; label = keep as a label; ignore = drop it"
                       % k}
        for k, p in asked.items()}


def from_form(content: Dict[str, Any], asked: Dict[str, List[Dict[str, str]]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in asked:
        v = content.get("keyTag.%s" % k)
        if v not in KEY_ANSWERS:
            raise ToolError("No valid answer for the tag @%s; nothing was written and nothing "
                            "was remembered." % k, keys=list(asked))
        out[k] = v
    return out
