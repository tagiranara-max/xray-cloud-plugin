"""Shared implementation for test sets and test plans (both are containers of tests)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..context import (ServerContext, check_issue_in_project, dedupe, delete_preview,
                       partial_failure, permission_guard, require_project)
from ..errors import ToolError
from ..fields import build_jira_fields
from ..registry import REGISTRY
from .tests import (_AI_LABEL_PARAMS, _LIMIT, _OBJ, MAX_LIMIT, TEST_TYPES, _schema,
                    build_search_jql, check_ai_label, create_test, find_duplicates,
                    remember_ai_label, resolve_ai_label, RESOLVED_AI_LABEL)

PAGE = 100


class Kind:
    def __init__(self, cap: str, label: str) -> None:
        self.cap = cap                      # "TestSet"
        self.item = cap[0].lower() + cap[1:]  # "testSet" (field-mapping item type)
        self.label = label                  # "test set"
        self.snake = label.replace(" ", "_")  # "test_set"
        self.plural = cap + "s"

    def q(self, template: str) -> str:
        return template.replace("@Cap", self.cap).replace("@item", self.item)


_SEARCH = """
query Get@Caps($jql: String!, $limit: Int!) {
  get@Caps(jql: $jql, limit: $limit) {
    results { issueId jira(fields: ["key", "summary"]) }
  }
}
"""
_MEMBERS = """
query GetTestsIn@Cap($jql: String!, $testLimit: Int!, $testStart: Int!) {
  get@Caps(jql: $jql, limit: 1) {
    results {
      issueId
      jira(fields: ["key", "summary"])
      tests(limit: $testLimit, start: $testStart) {
        total
        results { issueId testType { name } jira(fields: ["key", "summary"]) }
      }
    }
  }
}
"""
_CREATE = """
mutation Create@Cap($jira: JSON!) {
  create@Cap(jira: $jira) {
    @item { issueId jira(fields: ["key"]) }
    warnings
  }
}
"""
_ADD = """
mutation AddTestsTo@Cap($issueId: String!, $testIssueIds: [String!]!) {
  addTestsTo@Cap(issueId: $issueId, testIssueIds: $testIssueIds) { addedTests warning }
}
"""
_REMOVE = """
mutation RemoveTestsFrom@Cap($issueId: String!, $testIssueIds: [String!]!) {
  removeTestsFrom@Cap(issueId: $issueId, testIssueIds: $testIssueIds)
}
"""
_DELETE = """
mutation Delete@Cap($issueId: String!) { delete@Cap(issueId: $issueId) }
"""
_TEST_IDS = """
query GetTestIssueIds($jql: String!, $limit: Int!) {
  getTests(jql: $jql, limit: $limit) { results { issueId jira(fields: ["key"]) } }
}
"""


def _key(r: dict) -> Optional[str]:
    return (r.get("jira") or {}).get("key")


def fetch_container(ctx: ServerContext, kind: Kind, pk: str, key: str,
                    limit: int = 1, start: int = 0) -> dict:
    """{issueId, key, summary, total, tests: [{key, summary, testType, issueId}]} or raises."""
    check_issue_in_project(key, pk)
    data = ctx.xray.graphql(kind.q(_MEMBERS), {
        "jql": "project = %s AND issue = %s" % (pk, key), "testLimit": limit, "testStart": start})
    results = ((data.get("get" + kind.plural) or {}).get("results")) or []
    if not results:
        raise ToolError("%s not found: %s" % (kind.label.capitalize(), key), key=key)
    c = results[0]
    tests = c.get("tests") or {}
    rows = []
    for t in tests.get("results") or []:
        j = t.get("jira") or {}
        tt = t.get("testType")
        rows.append({"key": j.get("key"), "summary": j.get("summary"),
                     "testType": tt.get("name") if isinstance(tt, dict) else tt,
                     "issueId": t.get("issueId")})
    return {"issueId": c["issueId"], "key": _key(c), "summary": (c.get("jira") or {}).get("summary"),
            "total": tests.get("total", len(rows)), "tests": rows}


def all_members(ctx: ServerContext, kind: Kind, pk: str, key: str) -> List[dict]:
    members: List[dict] = []
    start = 0
    while True:
        page = fetch_container(ctx, kind, pk, key, PAGE, start)
        members.extend(page["tests"])
        start += PAGE
        if not page["tests"] or len(members) >= page["total"]:
            return members


def chunks(seq: List[Any], size: int = MAX_LIMIT) -> List[List[Any]]:
    """seq split into lists of at most `size` items (Xray returns at most 100 per call)."""
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def resolve_test_id_map(ctx: ServerContext, pk: str, test_keys: List[str]) -> Dict[str, str]:
    """{key: issueId} for test keys of the project, in input order; raises for unknown keys.
    Looked up in batches of at most 100 keys."""
    for k in test_keys:
        check_issue_in_project(k, pk)
    test_keys = dedupe(test_keys)
    found: Dict[str, str] = {}
    for batch in chunks(test_keys):
        data = ctx.xray.graphql(_TEST_IDS, {"jql": "issue in (%s)" % ", ".join(batch),
                                            "limit": len(batch)})
        for r in ((data.get("getTests") or {}).get("results")) or []:
            found[_key(r)] = r["issueId"]
    missing = [k for k in test_keys if k not in found]
    if missing:
        raise ToolError("Test keys not found in Xray: %s" % ", ".join(missing), missing=missing)
    return {k: found[k] for k in test_keys}


def resolve_test_ids(ctx: ServerContext, pk: str, test_keys: List[str]) -> List[str]:
    m = resolve_test_id_map(ctx, pk, test_keys)
    return [m[k] for k in test_keys]


def keys_for(ids: List[Any], keys_by_id: Dict[str, str]) -> List[Any]:
    """Map Xray issue ids back to issue keys (an unknown id is kept as it is)."""
    return [keys_by_id.get(str(i), i) for i in ids]


def _check_keys(pk: str, *groups: Optional[List[str]]) -> None:
    for g in groups:
        for k in g or []:
            check_issue_in_project(k, pk)


def _add(ctx: ServerContext, kind: Kind, issue_id: str, test_ids: List[str],
         keys_by_id: Optional[Dict[str, str]] = None) -> dict:
    """addTestsTo<Kind>; "added" lists issue keys when keys_by_id (issueId -> key) is given."""
    if not test_ids:
        return {"added": [], "warnings": []}
    data = ctx.xray.graphql(kind.q(_ADD), {"issueId": issue_id, "testIssueIds": test_ids})
    r = data.get("addTestsTo" + kind.cap) or {}
    return {"added": keys_for(r.get("addedTests") or [], keys_by_id or {}),
            "warnings": [r["warning"]] if r.get("warning") else []}


def _create_new_tests(ctx: ServerContext, pk: str, new_tests: List[dict],
                      create_dups: List[int], created: List[str],
                      ai_args: Optional[Dict[str, Any]] = None):
    """Create newTests (skipping duplicates unless their index is in create_dups).
    Appends created keys to `created`; returns ({issueId: key}, skipped)."""
    ids: Dict[str, str] = {}
    skipped: List[dict] = []
    for i, nt in enumerate(new_tests):
        if i not in create_dups:
            dups = find_duplicates(ctx, pk, nt["summary"])
            if dups:
                skipped.append({"index": i, "summary": nt["summary"], "candidates": dups})
                continue
        a = {"projectKey": pk, "allowDuplicates": True}
        a.update(ai_args or {})
        a.update({k: v for k, v in nt.items() if v is not None})
        res = create_test(ctx, a)
        created.append(res["key"])
        ids.update({i: k for k, i in resolve_test_id_map(ctx, pk, [res["key"]]).items()})
    return ids, skipped


_NEW_TESTS = {"type": "array", "items": {"type": "object", "properties": {
    "summary": {"type": "string"}, "description": {"type": "string"},
    "testType": {"type": "string", "enum": TEST_TYPES}, "gherkin": {"type": "string"}},
    "required": ["summary"], "additionalProperties": False}}
_KEYS = {"type": "array", "items": {"type": "string"}}


def register(kind: Kind, with_sets: bool) -> None:
    label, snake, cap = kind.label, kind.snake, kind.cap
    key_param = kind.item + "Key"

    def members_of(ctx, pk, args) -> tuple:
        """Test keys + the (key, issueId) of members, from testKeys and (plans) testSetKeys."""
        from .test_sets import SET
        _check_keys(pk, args.get("testKeys"), args.get("testSetKeys"))
        direct = list(args.get("testKeys") or [])
        via_sets: List[dict] = []
        for sk in args.get("testSetKeys") or []:
            via_sets.extend(all_members(ctx, SET, pk, sk))
        return direct, via_sets

    # ---- create
    props: Dict[str, Any] = {
        "summary": {"type": "string"}, "description": {"type": "string"},
        "testKeys": _KEYS, "newTests": _NEW_TESTS,
        "createDuplicates": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "fields": _OBJ, "rawFields": _OBJ}
    if with_sets:
        props["testSetKeys"] = _KEYS
    props.update(_AI_LABEL_PARAMS)  # applies to newTests only

    @REGISTRY.tool(
        "create_" + snake,
        "Create a %s, optionally adding existing tests (testKeys%s) and creating new ones "
        "(newTests). New tests whose summary matches an existing test are skipped and reported "
        "unless their 0-based index is listed in createDuplicates. If newTests are given and the "
        "result has status needs_answer (with `questions`; it asks for the AI label), ask the "
        "user once and call again with the answer; it is remembered for the project." %
        (label, ", testSetKeys" if with_sets else ""),
        _schema(props, ["summary"]), write=True)
    def create(ctx, args):
        pk, summary = args["projectKey"], args["summary"]
        project = require_project(ctx, pk, write=True)
        extra = build_jira_fields(project, kind.item, args.get("fields"), args.get("rawFields"))
        new_tests = args.get("newTests") or []
        ai_args: Dict[str, Any] = {}
        if new_tests:  # the AI label applies to newTests only; otherwise it is not applicable
            check_ai_label(args.get("aiLabel"))
            label, needs_answer = resolve_ai_label(ctx, pk, args, {"newTests": len(new_tests)})
            if needs_answer:
                return needs_answer
            ai_args = {RESOLVED_AI_LABEL: label}
        direct, via_sets = members_of(ctx, pk, args)
        create_dups = args.get("createDuplicates") or []
        bad = [i for i in create_dups if i >= len(new_tests)]
        if bad:
            raise ToolError("createDuplicates index out of range: %s" % bad)
        if new_tests:
            # create_test applies the project's test fields (required/defaults) to every new
            # test: check them now so a missing required field cannot orphan the container.
            build_jira_fields(project, "test", None, None)
        direct_map = resolve_test_id_map(ctx, pk, direct)  # fail before creating anything
        if new_tests:  # all validation passed: remember a call-supplied label before writing
            remember_ai_label(ctx, pk, args)
        jira_fields: Dict[str, Any] = {"project": {"key": pk}, "summary": summary}
        if args.get("description") is not None:
            jira_fields["description"] = args["description"]
        jira_fields.update(extra)
        data = ctx.xray.graphql(kind.q(_CREATE), {"jira": {"fields": jira_fields}})
        node = (data.get("create" + cap) or {}).get(kind.item) or {}
        key = _key(node)
        if not key:
            raise ToolError("Xray did not return a key for the created %s" % label)
        out: Dict[str, Any] = {"key": key, "summary": summary, "created": True}
        warnings = list((data.get("create" + cap) or {}).get("warnings") or [])
        created = [key]
        try:
            new_ids, skipped = _create_new_tests(ctx, pk, new_tests, create_dups, created,
                                                  ai_args)
            keys_by_id = {i: k for k, i in direct_map.items()}
            keys_by_id.update({m["issueId"]: m["key"] for m in via_sets})
            keys_by_id.update(new_ids)
            ids = list(keys_by_id)
            if ids:
                res = _add(ctx, kind, node["issueId"], ids, keys_by_id)
                out["addedTests"] = res["added"]
                warnings.extend(res["warnings"])
        except Exception as e:
            raise partial_failure("%s %s was created but a later step failed" % (cap, key),
                                  created, e)
        if len(created) > 1:
            out["createdTests"] = created[1:]
        if skipped:
            out["skippedDuplicates"] = skipped
            out["hint"] = "pass the skipped indexes in createDuplicates to create them anyway"
        if warnings:
            out["warnings"] = warnings
        return out

    # ---- search
    @REGISTRY.tool(
        "search_" + snake + "s",
        "Search %ss in a project by summary text or issue key (query) and/or a JQL filter (jql). "
        "At least one of query or jql is required." % label,
        _schema({"query": {"type": "string"}, "jql": {"type": "string"},
                 "limit": _LIMIT}, []))
    def search(ctx, args):
        pk = args["projectKey"]
        require_project(ctx, pk, write=False)
        if not args.get("query") and not args.get("jql"):
            raise ToolError("Provide query or jql")
        data = ctx.xray.graphql(kind.q(_SEARCH), {
            "jql": build_search_jql(pk, args.get("query"), args.get("jql")),
            "limit": args.get("limit", 20)})
        rows = ((data.get("get" + kind.plural) or {}).get("results")) or []
        return {kind.item + "s": [{"key": _key(r), "summary": (r.get("jira") or {}).get("summary")}
                                  for r in rows if str(_key(r)).startswith(pk + "-")]}

    # ---- get tests
    @REGISTRY.tool(
        "get_tests_in_" + snake,
        "List the tests in a %s (paginated with limit and start)." % label,
        _schema({key_param: {"type": "string"}, "limit": _LIMIT,
                 "start": {"type": "integer", "minimum": 0}}, [key_param]))
    def get_tests(ctx, args):
        pk = args["projectKey"]
        require_project(ctx, pk, write=False)
        start = args.get("start", 0)
        c = fetch_container(ctx, kind, pk, args[key_param], args.get("limit", PAGE), start)
        return {"key": c["key"], "summary": c["summary"], "total": c["total"], "start": start,
                "tests": [{k: t[k] for k in ("key", "summary", "testType")} for t in c["tests"]]}

    # ---- add / remove
    mem_props: Dict[str, Any] = {key_param: {"type": "string"}, "testKeys": _KEYS}
    if with_sets:
        mem_props["testSetKeys"] = _KEYS

    def need_some(args):
        if not args.get("testKeys") and not args.get("testSetKeys"):
            raise ToolError("Provide at least one of: testKeys%s" %
                            (", testSetKeys" if with_sets else ""))

    @REGISTRY.tool(
        "add_tests_to_" + snake,
        "Add tests to a %s%s." % (label, " (testSetKeys adds all tests of those test sets)"
                                 if with_sets else ""),
        _schema(mem_props, [key_param] if with_sets else [key_param, "testKeys"]), write=True)
    def add_tests(ctx, args):
        pk = args["projectKey"]
        require_project(ctx, pk, write=True)
        need_some(args)
        direct, via_sets = members_of(ctx, pk, args)
        c = fetch_container(ctx, kind, pk, args[key_param])
        keys_by_id = {i: k for k, i in resolve_test_id_map(ctx, pk, direct).items()}
        keys_by_id.update({m["issueId"]: m["key"] for m in via_sets})
        res = _add(ctx, kind, c["issueId"], list(keys_by_id), keys_by_id)
        out = {"key": c["key"], "added": res["added"]}
        if args.get("testSetKeys"):
            out["fromTestSets"] = args["testSetKeys"]
        if res["warnings"]:
            out["warnings"] = res["warnings"]
        return out

    @REGISTRY.tool(
        "remove_tests_from_" + snake,
        "Remove tests from a %s. Tests that are not members are reported as notMember (not an "
        "error)." % label,
        _schema(mem_props, [key_param] if with_sets else [key_param, "testKeys"]), write=True)
    def remove_tests(ctx, args):
        pk = args["projectKey"]
        require_project(ctx, pk, write=True)
        need_some(args)
        direct, via_sets = members_of(ctx, pk, args)
        c = fetch_container(ctx, kind, pk, args[key_param])
        members = {m["key"]: m["issueId"] for m in all_members(ctx, kind, pk, args[key_param])}
        wanted = dedupe(direct + [m["key"] for m in via_sets])
        present = [k for k in wanted if k in members]
        not_member = [k for k in wanted if k not in members]
        if present:
            ctx.xray.graphql(kind.q(_REMOVE), {"issueId": c["issueId"],
                                               "testIssueIds": [members[k] for k in present]})
        return {"key": c["key"], "removed": present, "notMember": not_member}

    # ---- delete
    @REGISTRY.tool(
        "delete_" + snake,
        "Delete a %s (the tests themselves are kept). Without confirm: true it only previews "
        "what would be deleted." % label,
        _schema({key_param: {"type": "string"}, "confirm": {"type": "boolean"}}, [key_param]),
        write=True)
    def delete(ctx, args):
        pk = args["projectKey"]
        require_project(ctx, pk, write=True)
        c = fetch_container(ctx, kind, pk, args[key_param])
        preview = delete_preview(ctx, args.get("confirm", False),
                                 {"key": c["key"], "summary": c["summary"],
                                  "testCount": c["total"]}, pk)
        if preview is not None:
            return preview
        with permission_guard("delete", pk, c["key"]):
            ctx.xray.graphql(kind.q(_DELETE), {"issueId": c["issueId"]})
        return {"key": c["key"], "deleted": True}
