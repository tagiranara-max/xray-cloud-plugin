from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Optional

from ..context import (ISSUE_KEY_RE, ServerContext, check_issue_in_project, compact,
                       delete_preview, jql_text, partial_failure, permission_guard,
                       require_project, resolve_project_id)
from .. import choices
from ..errors import ToolError
from .. import requiredfields
from ..fields import build_jira_fields
from ..folderops import add_ids_to_folder, ensure_folder, normalize_folder_path
from ..notdone import NO_JIRA_FIX, NO_JIRA_REASON
from ..registry import REGISTRY

JIRA_TOKEN_MESSAGE = "Not done: %s. To fix: %s (see README.md#optional-jira-token)." % (
    NO_JIRA_REASON, NO_JIRA_FIX)
TEST_TYPES = ["Manual", "Cucumber", "Generic"]
_JIRA_ONLY = ("summary", "description", "labels", "priority", "assignee", "fields", "rawFields")

_GET_TESTS = """
query SearchTests($jql: String!, $limit: Int!) {
  getTests(jql: $jql, limit: $limit) {
    total
    results {
      issueId
      testType { name }
      %sjira(fields: %s)
    }
  }
}
"""
_BASE_JIRA = ["key", "summary", "status", "labels"]
_DETAIL_JIRA = _BASE_JIRA + ["description", "priority", "assignee"]


def _jira_list(names: List[str]) -> str:
    return "[" + ", ".join('"%s"' % n for n in names) + "]"


_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


def _scan(jql: str):
    """Yield (index, char, in_quote) for each char of jql."""
    quote = None
    i = 0
    while i < len(jql):
        c = jql[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
            yield i, c, True
        else:
            if c in "\"'":
                quote = c
                yield i, c, True
            else:
                yield i, c, False
        i += 1


def _split_order_by(jql: str):
    for m in _ORDER_BY.finditer(jql):
        if not any(q for idx, _, q in _scan(jql) if idx == m.start()):
            return jql[:m.start()].strip(), jql[m.start():].strip()
    return jql.strip(), ""


def _check_balanced(jql: str) -> None:
    depth = 0
    for _, c, quoted in _scan(jql):
        if quoted:
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        raise ToolError("jql has unbalanced parentheses")


def build_search_jql(project_key: str, query: Optional[str] = None, jql: Optional[str] = None) -> str:
    """Always scoped to the project. Issue-key queries become `issue = KEY`; other text is a
    quote-escaped summary search; user jql is paren-wrapped (ORDER BY re-appended last)."""
    parts = ["project = %s" % project_key]
    order = ""
    if query:
        if ISSUE_KEY_RE.fullmatch(query):
            parts.append("issue = %s" % query)
        else:
            term = jql_text(query)
            if term is None:
                raise ToolError("query has no searchable text", query=query)
            parts.append("summary ~ %s" % term)
    if jql:
        cond, order = _split_order_by(jql)
        _check_balanced(cond)
        if cond:
            parts.append("(%s)" % cond)
    out = " AND ".join(parts)
    return out + " " + order if order else out


def _shape(result: dict) -> dict:
    jira = dict(result.get("jira") or {})
    out = dict(jira)
    status = jira.get("status")
    if isinstance(status, dict):
        out["status"] = status.get("name")
    for k in ("priority", "assignee"):
        v = jira.get(k)
        if isinstance(v, dict):
            out[k] = v.get("name") or v.get("displayName") or v.get("accountId")
    tt = result.get("testType")
    if tt:
        out["testType"] = tt.get("name") if isinstance(tt, dict) else tt
    return out


def _search(ctx: ServerContext, jql: str, limit: int, detail: bool,
            jira_fields: List[str] = ()) -> List[dict]:
    extra = "gherkin steps { action data result } " if detail else ""
    names = list(_DETAIL_JIRA if detail else _BASE_JIRA)
    names += [f for f in jira_fields if f not in names]
    data = ctx.xray.graphql(_GET_TESTS % (extra, _jira_list(names)),
                            {"jql": jql, "limit": limit})
    return ((data.get("getTests") or {}).get("results")) or []


def find_duplicates(ctx: ServerContext, project_key: str, summary: str) -> List[dict]:
    """Tests in the project whose summary matches `summary` (JQL `~`); [] when
    settings.duplicate_check is false. Returns [{key, summary}]."""
    if not ctx.settings_store.get().duplicate_check:
        return []
    term = jql_text(summary)
    if term is None:
        return []
    jql = "project = %s AND summary ~ %s" % (project_key, term)
    out = []
    for r in _search(ctx, jql, 20, False):
        j = r.get("jira") or {}
        out.append({"key": j.get("key"), "summary": j.get("summary")})
    return out


def fetch_test(ctx: ServerContext, project_key: str, test_key: str, detail: bool = False,
               jira_fields: List[str] = ()) -> dict:
    """Validated lookup: returns the raw getTests result (issueId, testType, jira) or raises.
    jira_fields: extra Jira field ids to read (e.g. custom fields)."""
    check_issue_in_project(test_key, project_key)
    results = _search(ctx, "issue = %s" % test_key, 1, detail, jira_fields)
    if not results:
        raise ToolError("Test not found: %s" % test_key, testKey=test_key)
    return results[0]


def require_jira(ctx: ServerContext) -> Any:
    if ctx.jira is None:
        raise ToolError(JIRA_TOKEN_MESSAGE, reason=NO_JIRA_REASON, fix=NO_JIRA_FIX)
    return ctx.jira


def _project_param() -> dict:
    return {"type": "string", "description": "Jira project key, e.g. PROJ"}


def _schema(props: dict, required: List[str]) -> dict:
    props = dict(props)
    props["projectKey"] = _project_param()
    return {"type": "object", "properties": props, "required": ["projectKey"] + required,
            "additionalProperties": False}


MAX_LIMIT = 100  # Xray's page size limit
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}
_STEPS = {"type": "array", "items": {"type": "object", "properties": {
    "action": {"type": "string"},
    "data": {"type": "string", "description": "Test data (optional)"},
    "result": {"type": "string", "description": "Expected result (optional; Xray allows empty)"}},
    "required": ["action"], "additionalProperties": False,
    "description": "A step: action is required; result and data are optional"}}
_OBJ = {"type": "object"}


@REGISTRY.tool(
    "search_tests",
    "Search tests in a project by summary text or issue key (query) and/or a JQL filter (jql). "
    "At least one of query or jql is required. Compact results unless detail=true.",
    _schema({"query": {"type": "string"}, "jql": {"type": "string"},
             "limit": _LIMIT, "detail": {"type": "boolean"}}, []))
def search_tests(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=False)
    if not args.get("query") and not args.get("jql"):
        raise ToolError("Provide query or jql")
    detail = args.get("detail", False)
    results = _search(ctx, build_search_jql(pk, args.get("query"), args.get("jql")),
                      args.get("limit", 50), detail)
    results = [r for r in results if str((r.get("jira") or {}).get("key", "")).startswith(pk + "-")]
    return {"tests": [compact(_shape(r), detail) for r in results]}


@REGISTRY.tool(
    "get_test",
    "Get one test by key. Compact (key, summary, status, testType, labels) unless detail=true, "
    "which also returns description, priority, assignee, steps and gherkin.",
    _schema({"testKey": {"type": "string"}, "detail": {"type": "boolean"}}, ["testKey"]))
def get_test(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    require_project(ctx, pk, write=False)
    detail = args.get("detail", False)
    r = fetch_test(ctx, pk, key, detail)
    out = _shape(r)
    if detail:
        for k in ("steps", "gherkin"):
            if r.get(k) is not None:
                out[k] = r[k]
    return compact(out, detail)


_CREATE_TEST = """
mutation CreateTest($jira: JSON!, $testType: UpdateTestTypeInput, $gherkin: String,
                    $steps: [CreateStepInput]) {
  createTest(jira: $jira, testType: $testType, gherkin: $gherkin, steps: $steps) {
    test { issueId testType { name } jira(fields: ["key", "summary"]) }
    warnings
  }
}
"""


# Internal arg (not in the schema): a container's already-resolved label; never remembered.
RESOLVED_AI_LABEL = "_resolvedAiLabel"

_REMEMBER = {"type": "boolean", "description":
             "Make a differing value of a required field replace the remembered one (default: "
             "remembered only when nothing is remembered for that field yet)."}

_AI_LABEL_PARAMS = {
    "aiLabel": {"type": "string", "description":
                "The user's own label that marks newly created tests as AI-created; there is no "
                "default. Empty string = don't mark. Asked once per project, then remembered."},
    "remember": {"type": "boolean", "description":
                 "Make a differing aiLabel, or a differing value of a required field, replace "
                 "the remembered one (default: this call only)."}}


def check_ai_label(label: Any) -> None:
    """A given aiLabel must be a string with no whitespace; "" means don't mark."""
    if label is None:
        return
    if not isinstance(label, str) or any(c.isspace() for c in label):
        raise ToolError("aiLabel must be a label without whitespace, or an empty string to not "
                        "mark new tests", aiLabel=label)


def resolve_ai_label(ctx: ServerContext, pk: str, args: dict, context: Dict[str, Any]):
    """(label, needs_answer): label is the resolved aiLabel ("" = don't mark); needs_answer is
    the result to return when it is unanswered. Persists nothing: call remember_ai_label once
    all validation has passed, just before the first write."""
    value, _ = choices.resolve(ctx, pk, "aiLabel", args.get("aiLabel"))
    if value is None:
        return None, {"status": "needs_answer",
                      "questions": [choices.question("aiLabel", context)]}
    return value, None


def remember_ai_label(ctx: ServerContext, pk: str, args: dict) -> None:
    """Remember a call-supplied aiLabel (never a settings or remembered value)."""
    choices.remember(ctx, pk, {"aiLabel": args.get("aiLabel")},
                     bool(args.get("remember", False)))


def add_label(jira_fields: Dict[str, Any], label: str) -> None:
    """Add label to jira_fields["labels"], merged with labels already there, no duplicates."""
    if not label:
        return
    existing = list(jira_fields.get("labels") or [])
    if label not in existing:
        existing.append(label)
    jira_fields["labels"] = existing


@REGISTRY.tool(
    "create_test",
    "Create a test (Manual, Cucumber or Generic) in a project. Checks for duplicate summaries first "
    "and returns status possible_duplicates without creating unless allowDuplicates is true. "
    "Optional steps are created with the test (Manual); gherkin makes it a Cucumber test unless "
    "testType says otherwise (gherkin and steps are mutually exclusive); folderPath files it "
    "into a Test Repository folder. "
    "If the result has status needs_answer (with `questions`; it asks for the AI label), ask "
    "the user once and call again with the answer; it is remembered for the project.",
    _schema({"summary": {"type": "string"}, "description": {"type": "string"},
             "testType": {"type": "string", "enum": TEST_TYPES},
             "gherkin": {"type": "string"}, "steps": _STEPS, "fields": _OBJ, "rawFields": _OBJ,
             "allowDuplicates": {"type": "boolean"}, "folderPath": {"type": "string"},
             **_AI_LABEL_PARAMS},
            ["summary"]),
    write=True)
def create_test(ctx, args):
    pk, summary = args["projectKey"], args["summary"]
    project = require_project(ctx, pk, write=True)
    has_gherkin = args.get("gherkin") is not None
    if has_gherkin and args.get("steps"):
        raise ToolError("Pass either gherkin (Cucumber test) or steps (Manual test), not both")
    folder_path = None
    if args.get("folderPath") is not None:  # validate before any API call
        folder_path = normalize_folder_path(args["folderPath"], allow_root=False)
    check_ai_label(args.get("aiLabel"))
    inner = RESOLVED_AI_LABEL in args  # called by a container that already resolved/remembered
    if inner:
        label = args[RESOLVED_AI_LABEL]
    else:
        label, needs_answer = resolve_ai_label(ctx, pk, args, {"summary": summary})
        if needs_answer:
            return needs_answer
    if not args.get("allowDuplicates", False):
        dups = find_duplicates(ctx, pk, summary)
        if dups:
            return {"status": "possible_duplicates", "candidates": dups,
                    "hint": "pass allowDuplicates: true to create anyway"}
    # Required fields last, so an answer given by elicitation is not lost to an earlier
    # question of this call.
    extra, applied = requiredfields.resolve(
        ctx, project, "test", args.get("fields"), args.get("rawFields"),
        bool(args.get("remember", False)),
        provided=["description"] if args.get("description") is not None else [])
    if not inner:
        remember_ai_label(ctx, pk, args)
    requiredfields.commit(ctx)
    jira_fields: Dict[str, Any] = {"project": {"key": pk}, "summary": summary}
    if args.get("description") is not None:
        jira_fields["description"] = args["description"]
    jira_fields.update(extra)
    add_label(jira_fields, label)
    test_type = args.get("testType", "Cucumber" if has_gherkin else "Manual")
    variables: Dict[str, Any] = {"jira": {"fields": jira_fields},
                                 "testType": {"name": test_type}}
    if has_gherkin:
        variables["gherkin"] = args["gherkin"]
    if args.get("steps"):
        variables["steps"] = [{"action": s["action"], "data": s.get("data", ""),
                               "result": s.get("result", "")} for s in args["steps"]]
    data = ctx.xray.graphql(_CREATE_TEST, variables)
    test = (data.get("createTest") or {}).get("test") or {}
    key = (test.get("jira") or {}).get("key")
    if not key:
        raise ToolError("Xray did not return a key for the created test")
    out = {"key": key, "summary": summary, "testType": test_type, "created": True}
    requiredfields.add_applied(out, "test", applied)
    warnings = (data.get("createTest") or {}).get("warnings")
    if warnings:
        out["warnings"] = warnings
    if folder_path is not None:
        path = folder_path
        try:
            project_id = resolve_project_id(ctx, pk)
            ensure_folder(ctx, project_id, path)
            add_ids_to_folder(ctx, project_id, path, [test["issueId"]])
        except Exception as e:
            raise partial_failure("Test %s was created but adding it to folder %s failed"
                                  % (key, args["folderPath"]), [key], e)
        out["folderPath"] = path
    return out


_UPDATE_TYPE = """
mutation UpdateTestType($issueId: String!, $testType: UpdateTestTypeInput!) {
  updateTestType(issueId: $issueId, testType: $testType) {
    issueId
    testType { name }
  }
}
"""


@REGISTRY.tool(
    "update_test_type",
    "Change a test's type (Manual, Cucumber or Generic).",
    _schema({"testKey": {"type": "string"}, "testType": {"type": "string", "enum": TEST_TYPES}},
            ["testKey", "testType"]),
    write=True)
def update_test_type(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    require_project(ctx, pk, write=True)
    r = fetch_test(ctx, pk, key)
    ctx.step_cache.invalidate(key)
    ctx.xray.graphql(_UPDATE_TYPE, {"issueId": r["issueId"], "testType": {"name": args["testType"]}})
    return {"key": key, "testType": args["testType"], "updated": True}


_UPDATE_GHERKIN = """
mutation UpdateGherkin($issueId: String!, $gherkin: String!) {
  updateGherkinTestDefinition(issueId: $issueId, gherkin: $gherkin) { issueId }
}
"""


def _lenient(project, item_type):
    """Copy of project settings with no required/default, for partial edits."""
    specs = {n: dataclasses.replace(s, required=False, default=None)
             for n, s in (project.fields.get(item_type) or {}).items()}
    return dataclasses.replace(project, fields={item_type: specs})


@REGISTRY.tool(
    "edit_test",
    "Edit a test. gherkin is updated through Xray; summary, description, labels, priority, "
    "assignee, fields and rawFields are Jira fields and need the optional Jira API token.",
    _schema({"testKey": {"type": "string"}, "gherkin": {"type": "string"},
             "summary": {"type": "string"}, "description": {"type": "string"},
             "labels": {"type": "array", "items": {"type": "string"}},
             "priority": {"type": "string"}, "assignee": {"type": "string"},
             "fields": _OBJ, "rawFields": _OBJ}, ["testKey"]),
    write=True)
def edit_test(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    project = require_project(ctx, pk, write=True)
    check_issue_in_project(key, pk)
    has_jira = any(args.get(k) is not None for k in _JIRA_ONLY)
    has_gherkin = args.get("gherkin") is not None
    if not has_jira and not has_gherkin:
        raise ToolError("No fields provided to update")
    jira_fields: Dict[str, Any] = {}
    if has_jira:
        jira = require_jira(ctx)  # fail before any edit is applied
        for k in ("summary", "description", "labels"):
            if args.get(k) is not None:
                jira_fields[k] = args[k]
        if args.get("priority") is not None:
            jira_fields["priority"] = {"name": args["priority"]}
        if args.get("assignee") is not None:
            jira_fields["assignee"] = {"accountId": args["assignee"]}
        jira_fields.update(build_jira_fields(_lenient(project, "test"), "test",
                                             args.get("fields"), args.get("rawFields")))
    updated: List[str] = []
    if has_gherkin:
        r = fetch_test(ctx, pk, key)
        ctx.step_cache.invalidate(key)
        ctx.xray.graphql(_UPDATE_GHERKIN, {"issueId": r["issueId"], "gherkin": args["gherkin"]})
        updated.append("gherkin")
    if has_jira:
        try:
            jira.edit_issue(key, jira_fields)
        except Exception as e:
            if updated:
                raise partial_failure("Gherkin of %s was updated but the Jira fields were not"
                                      % key, [key], e)
            raise
        updated.extend(jira_fields)
    return {"key": key, "updated": updated}


_DELETE_TEST = """
mutation DeleteTest($issueId: String!) { deleteTest(issueId: $issueId) }
"""


@REGISTRY.tool(
    "delete_test",
    "Delete a test. Without confirm: true it only previews what would be deleted.",
    _schema({"testKey": {"type": "string"}, "confirm": {"type": "boolean"}}, ["testKey"]),
    write=True)
def delete_test(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    require_project(ctx, pk, write=True)
    r = fetch_test(ctx, pk, key)
    preview = delete_preview(ctx, args.get("confirm", False),
                             {"key": key, "summary": (r.get("jira") or {}).get("summary")}, pk)
    if preview is not None:
        return preview
    ctx.step_cache.invalidate(key)
    with permission_guard("delete", pk, key):
        ctx.xray.graphql(_DELETE_TEST, {"issueId": r["issueId"]})
    return {"key": key, "deleted": True}
