"""The feature import's question about existing tests a scenario maps to (feature import).

A scenario whose issue-key tag names a test of the import's project, or whose summary is
exactly that of one existing test of the project (no @TEST_ tag; matched by name), would
change that test (its steps, labels, fields, requirement links, Test Sets or
Pre-Conditions). That is never done silently: the import lists every such test with what
would change and asks once per import whether to update them, leave them unchanged (the
scenario only maps to the test), create a new test instead (tests matched by name only) or
skip those scenarios. Per-key overrides beat the general answer. Nothing is remembered: each
import asks when something would change."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import ToolError

PARAM = "existingTests"
OVERRIDES = "existingTestOverrides"
ANSWERS = ("update", "keep", "create", "skip")
TAG_ANSWERS = ("update", "keep", "skip")  # a test named by a key tag is never re-created
QUESTION_ID = "existingTests"

PARAM_SCHEMAS: Dict[str, Any] = {
    PARAM: {"type": "string", "enum": list(ANSWERS), "description":
            "Only after an existingTests question: the user's answer for existing tests the "
            "import would change. update = write the file's changes; keep = map the scenario "
            "to the test, write nothing to it; create = create a new test anyway (tests matched "
            "by name only; createDuplicates indexes mean the same); skip = leave those "
            "scenarios out. Never answer it yourself. Not remembered."},
    OVERRIDES: {"type": "object", "additionalProperties": {"type": "string",
                                                           "enum": list(ANSWERS)},
                "description": "Per-test answers {test key: update | keep | create | skip}; "
                               "they beat existingTests for that test."},
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
        raise ToolError("%s must be an object {test key: update | keep | create | skip}"
                        % OVERRIDES)
    keys: Dict[str, str] = {}
    for k, v in raw.items():
        if v not in ANSWERS:
            raise ToolError("%s.%s must be one of: %s" % (OVERRIDES, k, ", ".join(ANSWERS)))
        keys[str(k).strip().upper()] = v
    return {"all": general, "keys": keys}


def answer_for(answers: Dict[str, Any], key: str, by_name: bool = True) -> Optional[str]:
    """The answer for one test; "create" from the general answer only counts for a test
    matched by name (a test named by a key tag is then still unanswered)."""
    own = answers["keys"].get(key)
    if own:
        return own
    general = answers["all"]
    return None if general == "create" and not by_name else general


def by_name(change: Dict[str, Any]) -> bool:
    return change.get("matchedBy") == "name"


def check_overrides(changes: List[Dict[str, Any]], answers: Dict[str, Any]) -> None:
    """A per-test "create" for a test named by a key tag is refused."""
    for c in changes:
        if not by_name(c) and answers["keys"].get(c["key"]) == "create":
            raise ToolError("%s.%s: create only applies to tests matched by name; %s is named "
                            "by a key tag on scenario %r (update, keep or skip)"
                            % (OVERRIDES, c["key"], c["key"], c["scenario"]), testKey=c["key"])


def unanswered(changes: List[Dict[str, Any]], answers: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in changes if answer_for(answers, c["key"], by_name(c)) is None]


def _diff_text(d: Dict[str, Any]) -> str:
    return "+%d/-%d line(s)" % (d.get("added", 0), d.get("removed", 0))


def describe(change: Dict[str, Any]) -> str:
    """'P-5: steps changed, labels +a +b, ...' (compact)."""
    parts = ["matched by name" if by_name(change) else "named by a key tag"]
    if change.get("stepsChanged"):
        diff = change.get("stepsDiff")
        parts.append("steps changed" + (" (%s)" % _diff_text(diff) if diff else ""))
    else:
        parts.append("steps unchanged")
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


def _choices(changes: List[Dict[str, Any]]) -> List[str]:
    return list(ANSWERS) if any(by_name(c) for c in changes) else list(TAG_ANSWERS)


def question(changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    named = any(by_name(c) for c in changes)
    return {
        "id": QUESTION_ID, "param": PARAM,
        "question": ("Scenarios of this file name or match (same summary) existing tests, and "
                     "importing would change them: %s. update = change them from the file; "
                     "keep = leave them unchanged (the scenario maps to the existing test, "
                     "nothing is written to it); %sskip = leave these scenarios out. Answer "
                     "with existingTests, and optionally existingTestOverrides {test key: "
                     "answer} for single tests. Not remembered."
                     % ("; ".join(describe(c) for c in changes),
                        "create = create a new test instead (tests matched by name only); "
                        if named else "")),
        "choices": _choices(changes),
        "context": {"tests": [dict(c) for c in changes]}}


def form(changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Elicitation form properties (merged into the tag-rule form)."""
    props: Dict[str, Any] = {QUESTION_ID: {
        "type": "string", "enum": _choices(changes),
        "title": "Existing tests the import would change",
        "description": "update / keep (unchanged, mapped only) / %sskip the scenarios: "
                       % ("create (new test; matched by name only) / "
                          if any(by_name(c) for c in changes) else "")
                       + "; ".join(describe(c) for c in changes)}}
    for c in changes:
        props["%s.%s" % (QUESTION_ID, c["key"])] = {
            "type": "string", "enum": list(ANSWERS) if by_name(c) else list(TAG_ANSWERS),
            "title": "Only %s (optional)" % c["key"], "description": describe(c)}
    return props


def from_form(content: Dict[str, Any], changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    general = content.get(QUESTION_ID)
    keys = {c["key"]: content.get("%s.%s" % (QUESTION_ID, c["key"])) for c in changes}
    keys = {k: v for k, v in keys.items() if v in ANSWERS}
    check_overrides(changes, {"all": None, "keys": keys})
    answers = {"all": general if general in ANSWERS else None, "keys": keys}
    if unanswered(changes, answers):
        raise ToolError("The existing-tests question was not answered; nothing was written and "
                        "nothing was remembered.", tests=[c["key"] for c in changes])
    return {"all": general if general in ANSWERS else None, "keys": keys}
