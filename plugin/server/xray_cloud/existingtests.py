"""The feature import's question about existing tests a scenario maps to (feature import).

A scenario whose issue-key tag names a test of the import's project would change that test
(its steps, labels, fields, requirement links, Test Sets or Pre-Conditions). That is never
done silently: the import lists every such test with what would change and asks once per
import whether to update them, leave them unchanged (the scenario only maps to the test) or
skip those scenarios. Per-key overrides beat the general answer. Nothing is remembered: each
import asks when something would change."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import ToolError

PARAM = "existingTests"
OVERRIDES = "existingTestOverrides"
ANSWERS = ("update", "keep", "skip")
QUESTION_ID = "existingTests"

PARAM_SCHEMAS: Dict[str, Any] = {
    PARAM: {"type": "string", "enum": list(ANSWERS), "description":
            "Only after an existingTests question: the user's answer for existing tests the "
            "import would change. update = write the file's changes; keep = map the scenario "
            "to the test, write nothing to it; skip = leave those scenarios out. Never answer "
            "it yourself. Not remembered."},
    OVERRIDES: {"type": "object", "additionalProperties": {"type": "string",
                                                           "enum": list(ANSWERS)},
                "description": "Per-test answers {test key: update | keep | skip}; they beat "
                               "existingTests for that test."},
}


def parse(args: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the two parameters -> {"all": answer or None, "keys": {KEY: answer}}."""
    general = args.get(PARAM)
    if general is not None and general not in ANSWERS:
        raise ToolError("%s must be one of: %s" % (PARAM, ", ".join(ANSWERS)))
    raw = args.get(OVERRIDES)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolError("%s must be an object {test key: update | keep | skip}" % OVERRIDES)
    keys: Dict[str, str] = {}
    for k, v in raw.items():
        if v not in ANSWERS:
            raise ToolError("%s.%s must be one of: %s" % (OVERRIDES, k, ", ".join(ANSWERS)))
        keys[str(k).strip().upper()] = v
    return {"all": general, "keys": keys}


def answer_for(answers: Dict[str, Any], key: str) -> Optional[str]:
    return answers["keys"].get(key) or answers["all"]


def unanswered(changes: List[Dict[str, Any]], answers: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in changes if answer_for(answers, c["key"]) is None]


def describe(change: Dict[str, Any]) -> str:
    """'P-5: steps changed, labels +a +b, ...' (compact)."""
    parts = ["steps changed" if change.get("stepsChanged") else "steps unchanged"]
    if change.get("labelsAdded"):
        parts.append("labels " + " ".join("+" + x for x in change["labelsAdded"]))
    if change.get("fields"):
        parts.append("fields " + ", ".join(change["fields"]))
    if change.get("links"):
        parts.append("requirement links +" + ", +".join(change["links"]))
    if change.get("testSets"):
        parts.append("joins Test Set(s) " + ", ".join(change["testSets"]))
    if change.get("preconditions"):
        parts.append("Pre-Condition(s) " + ", ".join(change["preconditions"]))
    if change.get("pendingTags"):
        parts.append("tags awaiting a tag-rule answer: " + ", ".join(
            "@" + t for t in change["pendingTags"]))
    return "%s (%s): %s" % (change["key"], change["scenario"], "; ".join(parts))


def question(changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": QUESTION_ID, "param": PARAM,
        "question": ("Scenarios of this file name existing tests, and importing would change "
                     "them: %s. update = change them from the file; keep = leave them "
                     "unchanged (the scenario maps to the existing test, nothing is written to "
                     "it); skip = leave these scenarios out. Answer with existingTests, and "
                     "optionally existingTestOverrides {test key: answer} for single tests. "
                     "Not remembered." % "; ".join(describe(c) for c in changes)),
        "choices": list(ANSWERS),
        "context": {"tests": [dict(c) for c in changes]}}


def form(changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Elicitation form properties (merged into the tag-rule form)."""
    props: Dict[str, Any] = {QUESTION_ID: {
        "type": "string", "enum": list(ANSWERS), "title": "Existing tests the import would change",
        "description": "update / keep (unchanged, mapped only) / skip the scenarios: "
                       + "; ".join(describe(c) for c in changes)}}
    for c in changes:
        props["%s.%s" % (QUESTION_ID, c["key"])] = {
            "type": "string", "enum": list(ANSWERS), "title": "Only %s (optional)" % c["key"],
            "description": describe(c)}
    return props


def from_form(content: Dict[str, Any], changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    general = content.get(QUESTION_ID)
    keys = {c["key"]: content.get("%s.%s" % (QUESTION_ID, c["key"])) for c in changes}
    keys = {k: v for k, v in keys.items() if v in ANSWERS}
    if general not in ANSWERS and any(c["key"] not in keys for c in changes):
        raise ToolError("The existing-tests question was not answered; nothing was written and "
                        "nothing was remembered.", tests=[c["key"] for c in changes])
    return {"all": general if general in ANSWERS else None, "keys": keys}
