"""Test execution and test run tools."""
from __future__ import annotations

import base64
import mimetypes
import os
from typing import Any, Dict, List, Optional

from ..context import (PartialFailure, ServerContext, check_issue_key, dedupe,
                       delete_preview, partial_failure, permission_guard, require_project,
                       with_permission_reason)
from ..errors import ToolError
from .. import requiredfields
from ..fields import build_run_fields
from ..localfiles import PATH_NOTE, read_local_file
from ..registry import REGISTRY
from .containers import (_KEYS, _SEARCH, Kind, _add, _check_keys, _key, _REMOVE,
                         _DELETE, all_members, fetch_container, keys_for, resolve_test_id_map,
                         resolve_test_ids)
from .test_plans import PLAN
from .test_sets import SET
from .tests import _LIMIT, _OBJ, _REMEMBER, _lenient, _schema, build_search_jql

EXEC = Kind("TestExecution", "test execution")

_CREATE = """
mutation CreateTestExecution($jira: JSON!, $testEnvironments: [String]) {
  createTestExecution(jira: $jira, testEnvironments: $testEnvironments) {
    testExecution { issueId jira(fields: ["key"]) }
    warnings
  }
}
"""
_LINK_TO_PLAN = """
mutation AddTestExecutionsToTestPlan($issueId: String!, $testExecIssueIds: [String!]!) {
  addTestExecutionsToTestPlan(issueId: $issueId, testExecIssueIds: $testExecIssueIds) {
    addedTestExecutions
    warning
  }
}
"""
_EXEC_RUNS = """
query GetTestExecutionRuns($testExecIssueIds: [String], $limit: Int!, $start: Int!) {
  getTestRuns(testExecIssueIds: $testExecIssueIds, limit: $limit, start: $start) {
    total
    results {
      status { name }
      assigneeId
      executedById
      test { jira(fields: ["key", "summary"]) testType { name } }
      steps { status { name } comment action }
      defects
    }
  }
}
"""
_GET_RUN = """
query GetTestRuns($testExecIssueIds: [String], $testIssueIds: [String], $limit: Int!) {
  getTestRuns(testExecIssueIds: $testExecIssueIds, testIssueIds: $testIssueIds, limit: $limit) {
    results {
      id
      customFields { id name }
      steps { id status { name } }
    }
  }
}
"""
_UPDATE_RUN = """
mutation UpdateTestRun($id: String!, $assigneeId: String, $executedById: String,
                       $customFields: [CustomFieldInput]) {
  updateTestRun(id: $id, assigneeId: $assigneeId, executedById: $executedById,
                customFields: $customFields) {
    warnings
  }
}
"""
_UPDATE_STEP = """
mutation UpdateTestRunStep($testRunId: String!, $stepId: String!,
                           $updateData: UpdateTestRunStepInput!) {
  updateTestRunStep(testRunId: $testRunId, stepId: $stepId, updateData: $updateData) {
    warnings
  }
}
"""
_ADD_DEFECTS = """
mutation AddDefectsToTestRun($id: String!, $issues: [String]!) {
  addDefectsToTestRun(id: $id, issues: $issues) { addedDefects warnings }
}
"""
_ADD_EVIDENCE = """
mutation AddEvidenceToTestRun($id: String!, $evidence: [AttachmentDataInput]!) {
  addEvidenceToTestRun(id: $id, evidence: $evidence) { addedEvidence warnings }
}
"""
_UPDATE_STATUS = """
mutation UpdateTestRunStatus($id: String!, $status: String!) {
  updateTestRunStatus(id: $id, status: $status)
}
"""

_PASS = ("PASS", "PASSED")
_FAIL = ("FAIL", "FAILED")


def _members_ids(ctx: ServerContext, pk: str, args: dict, with_plan: bool) -> Dict[str, str]:
    """{issueId: key} for testKeys, then testSetKeys members, then (optionally) testPlanKey
    members, in that order without duplicates."""
    out = {i: k for k, i in resolve_test_id_map(ctx, pk, list(args.get("testKeys") or [])).items()}
    for sk in args.get("testSetKeys") or []:
        out.update((m["issueId"], m["key"]) for m in all_members(ctx, SET, pk, sk)
                   if m["issueId"] not in out)
    if with_plan and args.get("testPlanKey"):
        out.update((m["issueId"], m["key"]) for m in all_members(ctx, PLAN, pk, args["testPlanKey"])
                   if m["issueId"] not in out)
    return out


def _plan_keys(args: dict) -> List[str]:
    return [args["testPlanKey"]] if args.get("testPlanKey") else []


# ---- create

@REGISTRY.tool(
    "create_test_execution",
    "Create a test execution. environments must be registered Xray environments (see "
    "get_test_environments). With testPlanKey the execution is linked to the plan and, unless "
    "testKeys/testSetKeys are given, gets all of the plan's tests; testSetKeys are expanded to "
    "their member tests.",
    _schema({"summary": {"type": "string"}, "description": {"type": "string"},
             "environments": {"type": "array", "items": {"type": "string"}},
             "testPlanKey": {"type": "string"}, "testKeys": _KEYS, "testSetKeys": _KEYS,
             "fields": _OBJ, "rawFields": _OBJ, "remember": _REMEMBER}, ["summary"]),
    write=True)
def create_test_execution(ctx, args):
    pk, summary = args["projectKey"], args["summary"]
    project = require_project(ctx, pk, write=True)
    _check_keys(pk, args.get("testKeys"), args.get("testSetKeys"), _plan_keys(args))
    extra, applied = requiredfields.resolve(
        ctx, project, "testExecution", args.get("fields"), args.get("rawFields"),
        bool(args.get("remember", False)),
        provided=["description"] if args.get("description") is not None else [])
    plan = None
    explicit = bool(args.get("testKeys") or args.get("testSetKeys"))
    # Resolve everything before creating, so bad keys fail without side effects.
    test_ids = _members_ids(ctx, pk, args, with_plan=not explicit)
    if args.get("testPlanKey"):
        plan = fetch_container(ctx, PLAN, pk, args["testPlanKey"])
    jira_fields: Dict[str, Any] = {"project": {"key": pk}, "summary": summary}
    if args.get("description") is not None:
        jira_fields["description"] = args["description"]
    jira_fields.update(extra)
    with requiredfields.creating(ctx, pk, "testExecution"):
        data = ctx.xray.graphql(_CREATE, {"jira": {"fields": jira_fields},
                                          "testEnvironments": args.get("environments") or None})
    node = (data.get("createTestExecution") or {}).get("testExecution") or {}
    key = _key(node)
    if not key:
        raise ToolError("Xray did not return a key for the created test execution")
    out: Dict[str, Any] = {"key": key, "summary": summary, "created": True}
    requiredfields.add_applied(out, "testExecution", applied)
    warnings = list((data.get("createTestExecution") or {}).get("warnings") or [])
    try:
        if test_ids:
            res = _add(ctx, EXEC, node["issueId"], list(test_ids), test_ids)
            out["addedTests"] = res["added"]
            warnings.extend(res["warnings"])
        if plan is not None:
            r = ctx.xray.graphql(_LINK_TO_PLAN, {"issueId": plan["issueId"],
                                                 "testExecIssueIds": [node["issueId"]]})
            w = (r.get("addTestExecutionsToTestPlan") or {}).get("warning")
            if w:
                warnings.append(w)
            out["testPlanKey"] = plan["key"]
    except Exception as e:
        raise partial_failure("Test execution %s was created but a later step failed" % key,
                              [key], e)
    if args.get("environments"):
        out["environments"] = args["environments"]
    if warnings:
        out["warnings"] = warnings
    return out


# ---- link to plan

@REGISTRY.tool(
    "add_test_executions_to_test_plan",
    "Link existing test executions to a test plan (new executions: prefer create_test_execution "
    "with testPlanKey).",
    _schema({"testPlanKey": {"type": "string"},
             "testExecutionKeys": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
            ["testPlanKey", "testExecutionKeys"]),
    write=True)
def add_test_executions_to_test_plan(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=True)
    _check_keys(pk, [args["testPlanKey"]], args["testExecutionKeys"])
    plan = fetch_container(ctx, PLAN, pk, args["testPlanKey"])
    execs = {}
    for k in dedupe(args["testExecutionKeys"]):
        e = fetch_container(ctx, EXEC, pk, k)
        execs[e["issueId"]] = e["key"]
    data = ctx.xray.graphql(_LINK_TO_PLAN, {"issueId": plan["issueId"],
                                            "testExecIssueIds": list(execs)})
    r = data.get("addTestExecutionsToTestPlan") or {}
    out = {"testPlanKey": plan["key"],
           "addedTestExecutions": keys_for(r.get("addedTestExecutions") or [], execs)}
    if r.get("warning"):
        out["warnings"] = [r["warning"]]
    return out


# ---- membership

@REGISTRY.tool(
    "add_tests_to_test_execution",
    "Add tests to a test execution: testKeys, all tests of testSetKeys, and/or all tests of "
    "testPlanKey (the execution is NOT linked to that plan).",
    _schema({"testExecutionKey": {"type": "string"}, "testKeys": _KEYS, "testSetKeys": _KEYS,
             "testPlanKey": {"type": "string"}}, ["testExecutionKey"]),
    write=True)
def add_tests_to_test_execution(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=True)
    if not (args.get("testKeys") or args.get("testSetKeys") or args.get("testPlanKey")):
        raise ToolError("Provide at least one of: testKeys, testSetKeys, testPlanKey")
    _check_keys(pk, [args["testExecutionKey"]], args.get("testKeys"), args.get("testSetKeys"),
                _plan_keys(args))
    c = fetch_container(ctx, EXEC, pk, args["testExecutionKey"])
    members = _members_ids(ctx, pk, args, with_plan=True)
    res = _add(ctx, EXEC, c["issueId"], list(members), members)
    out: Dict[str, Any] = {"key": c["key"], "added": res["added"]}
    if args.get("testSetKeys"):
        out["fromTestSets"] = args["testSetKeys"]
    if args.get("testPlanKey"):
        out["fromTestPlan"] = args["testPlanKey"]
    if res["warnings"]:
        out["warnings"] = res["warnings"]
    return out


@REGISTRY.tool(
    "remove_tests_from_test_execution",
    "Remove tests (testKeys and/or all tests of testSetKeys) from a test execution. Tests that "
    "are not in the execution are reported as notMember (not an error).",
    _schema({"testExecutionKey": {"type": "string"}, "testKeys": _KEYS, "testSetKeys": _KEYS},
            ["testExecutionKey"]),
    write=True)
def remove_tests_from_test_execution(ctx, args):
    pk, ek = args["projectKey"], args["testExecutionKey"]
    require_project(ctx, pk, write=True)
    if not (args.get("testKeys") or args.get("testSetKeys")):
        raise ToolError("Provide at least one of: testKeys, testSetKeys")
    _check_keys(pk, [ek], args.get("testKeys"), args.get("testSetKeys"))
    wanted = list(args.get("testKeys") or [])
    for sk in args.get("testSetKeys") or []:
        wanted.extend(m["key"] for m in all_members(ctx, SET, pk, sk))
    wanted = dedupe(wanted)
    c = fetch_container(ctx, EXEC, pk, ek)
    members = {m["key"]: m["issueId"] for m in all_members(ctx, EXEC, pk, ek)}
    present = [k for k in wanted if k in members]
    if present:
        ctx.xray.graphql(EXEC.q(_REMOVE), {"issueId": c["issueId"],
                                           "testIssueIds": [members[k] for k in present]})
    return {"key": c["key"], "removed": present,
            "notMember": [k for k in wanted if k not in members]}


@REGISTRY.tool(
    "delete_test_execution",
    "Delete a test execution and its test runs (irreversible). Without confirm: true it only "
    "previews what would be deleted.",
    _schema({"testExecutionKey": {"type": "string"}, "confirm": {"type": "boolean"}},
            ["testExecutionKey"]),
    write=True)
def delete_test_execution(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=True)
    c = fetch_container(ctx, EXEC, pk, args["testExecutionKey"])
    preview = delete_preview(ctx, args.get("confirm", False),
                             {"key": c["key"], "summary": c["summary"],
                              "testCount": c["total"]}, pk)
    if preview is not None:
        return preview
    with permission_guard("delete", pk, c["key"]):
        ctx.xray.graphql(EXEC.q(_DELETE), {"issueId": c["issueId"]})
    return {"key": c["key"], "deleted": True}


# ---- search / get

@REGISTRY.tool(
    "search_test_executions",
    "Search test executions in a project by summary text or issue key (query) and/or a JQL "
    "filter (jql). At least one of query or jql is required.",
    _schema({"query": {"type": "string"}, "jql": {"type": "string"},
             "limit": _LIMIT}, []))
def search_test_executions(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=False)
    if not args.get("query") and not args.get("jql"):
        raise ToolError("Provide query or jql")
    data = ctx.xray.graphql(EXEC.q(_SEARCH), {
        "jql": build_search_jql(pk, args.get("query"), args.get("jql")),
        "limit": args.get("limit", 20)})
    rows = ((data.get("getTestExecutions") or {}).get("results")) or []
    return {"testExecutions": [{"key": _key(r), "summary": (r.get("jira") or {}).get("summary")}
                               for r in rows if str(_key(r)).startswith(pk + "-")]}


def _name(v: Any) -> Any:
    return v.get("name") if isinstance(v, dict) else v


def _run_row(r: dict, detail: bool) -> dict:
    test = r.get("test") or {}
    j = test.get("jira") or {}
    row: Dict[str, Any] = {"testKey": j.get("key"), "summary": j.get("summary"),
                           "status": _name(r.get("status"))}
    if detail:
        steps = r.get("steps") or []
        row.update({"testType": _name(test.get("testType")), "assigneeId": r.get("assigneeId"),
                    "executedById": r.get("executedById"), "defects": r.get("defects") or []})
        failed = [{"index": i, "action": s.get("action"), "status": _name(s.get("status")),
                   "comment": s.get("comment")}
                  for i, s in enumerate(steps)
                  if str(_name(s.get("status")) or "").upper() in _FAIL]
        if failed:
            row["failedSteps"] = failed
    return row


@REGISTRY.tool(
    "get_test_execution",
    "Get a test execution and its test runs (paginated with limit and start). Each run is "
    "compact (testKey, summary, status) unless detail=true, which adds testType, assigneeId, "
    "executedById, defects (Jira issue ids, not keys) and failedSteps (0-based index).",
    _schema({"testExecutionKey": {"type": "string"},
             "limit": _LIMIT, "start": {"type": "integer", "minimum": 0},
             "detail": {"type": "boolean"}}, ["testExecutionKey"]))
def get_test_execution(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=False)
    c = fetch_container(ctx, EXEC, pk, args["testExecutionKey"])
    start, detail = args.get("start", 0), args.get("detail", False)
    data = ctx.xray.graphql(_EXEC_RUNS, {"testExecIssueIds": [c["issueId"]],
                                         "limit": args.get("limit", 100), "start": start})
    runs = data.get("getTestRuns") or {}
    rows = runs.get("results") or []
    return {"key": c["key"], "summary": c["summary"], "total": runs.get("total", len(rows)),
            "start": start, "testRuns": [_run_row(r, detail) for r in rows]}


# ---- update_test_run

def normalise_status(status: str) -> str:
    """Upper-case; PASS -> PASSED, FAIL -> FAILED (Xray's built-in names). Others unchanged."""
    s = status.strip().upper()
    return {"PASS": "PASSED", "FAIL": "FAILED"}.get(s, s)


def derive_status(run_steps: List[dict], updates: List[dict],
                  explicit: Optional[str]) -> Optional[str]:
    """Explicit status (normalised) wins. Otherwise None (leave the run status alone) unless a
    step update carries a status; then the run status follows ALL steps of the run (their
    current status merged with this call's updates): any FAILED -> FAILED, all PASSED ->
    PASSED, otherwise EXECUTING."""
    if explicit:
        return normalise_status(explicit)
    if not any(u.get("status") is not None for u in updates):
        return None
    statuses = [normalise_status(str(_name(s.get("status")) or "TODO")) for s in run_steps]
    for u in updates:
        if u.get("status") is not None and 0 <= u["index"] < len(statuses):
            statuses[u["index"]] = normalise_status(u["status"])
    if any(s == "FAILED" for s in statuses):
        return "FAILED"
    if statuses and all(s == "PASSED" for s in statuses):
        return "PASSED"
    return "EXECUTING"


def _evidence(env, paths: List[str]) -> List[dict]:
    out = []
    for p in paths:
        full, data = read_local_file(env, p, "Evidence file")
        out.append({"filename": os.path.basename(full),
                    "mimeType": mimetypes.guess_type(full)[0] or "application/octet-stream",
                    "data": base64.b64encode(data).decode("ascii")})
    return out


def _resolve_run_fields(entries: List[dict], run: dict, labels: Dict[str, str]):
    """Map build_run_fields entries ({name|id, value}) to [{id, value}] using the run's fields."""
    defs = run.get("customFields") or []
    by_name = {d.get("name"): d.get("id") for d in defs}
    ids = {d.get("id") for d in defs}
    out, names, missing = [], [], []
    for e in entries:
        if "id" in e:
            fid, ref = e["id"], e["id"]
        else:
            ref = e["name"]
            fid = by_name.get(ref) or (ref if ref in ids else None)
            if fid is None:
                missing.append(ref)
                continue
        out.append({"id": fid, "value": e["value"]})
        names.append(labels.get(ref, ref))
    if missing:
        raise ToolError("Custom field(s) not found on this test run: %s" % ", ".join(missing),
                        missing=missing, available=sorted(n for n in by_name if n))
    return out, names


_RUN_STEPS = {"type": "array", "items": {"type": "object", "properties": {
    "index": {"type": "integer", "minimum": 0, "description": "0-based step index in the run"},
    "status": {"type": "string", "description": "e.g. PASSED | FAILED | TODO | EXECUTING "
                                                "(PASS/FAIL are sent as PASSED/FAILED)"},
    "comment": {"type": "string"}, "actualResult": {"type": "string"}},
    "required": ["index"], "additionalProperties": False}}
_UNASSIGN_STR = {"type": "string", "description":
                 "Jira account id; an empty string unassigns. Omit to leave unchanged."}


@REGISTRY.tool(
    "update_test_run",
    "Update the run of testKey in a test execution. status is optional: when omitted and a step "
    "update sets a step status, the run status is derived from all steps of the run (any FAIL "
    "-> FAILED, all PASS -> PASSED, otherwise EXECUTING); updates without any status (e.g. "
    "comments only) leave the run status unchanged. "
    "Other updates (assignee/executor, fields, steps, defects, evidence attachments) are applied "
    "before the status. assigneeId/executedById: Jira account id, or \"\" to unassign. fields "
    "uses friendly names from settings (required/default run fields are enforced when a status "
    "is set); rawFields is {Xray run field name or id: value}. attachments are local file paths.",
    _schema({"testExecutionKey": {"type": "string"}, "testKey": {"type": "string"},
             "status": {"type": "string"}, "assigneeId": _UNASSIGN_STR,
             "executedById": _UNASSIGN_STR, "fields": _OBJ, "rawFields": _OBJ,
             "steps": _RUN_STEPS, "attachments": {"type": "array", "items": {"type": "string"},
                             "description": "Local evidence file paths. " + PATH_NOTE},
             "defectKeys": _KEYS}, ["testExecutionKey", "testKey"]),
    write=True)
def update_test_run(ctx, args):
    pk, ek, tk = args["projectKey"], args["testExecutionKey"], args["testKey"]
    project = require_project(ctx, pk, write=True)
    _check_keys(pk, [ek, tk])
    for d in args.get("defectKeys") or []:
        check_issue_key(d)  # defects may live in another project (ruling R8): format only
    steps = args.get("steps") or []
    if args.get("status") is not None and not args["status"].strip():
        raise ToolError("status must not be empty")
    bad = [s["index"] for s in steps if s.get("status") is not None and not s["status"].strip()]
    if bad:
        raise ToolError("Step status must not be empty (step index %s)" % bad)
    dup = sorted({s["index"] for s in steps if [t["index"] for t in steps].count(s["index"]) > 1})
    if dup:
        raise ToolError("Duplicate step index(es): %s" % dup)
    sets_status = bool(args.get("status")) or any(s.get("status") is not None for s in steps)
    run_project = project if sets_status else _lenient(project, "testRun")
    entries = build_run_fields(run_project, args.get("fields"), args.get("rawFields"))
    labels = {(s.xray_name or s.id): n
              for n, s in (project.fields.get("testRun") or {}).items()}
    assign = {k: (args[k] or None) for k in ("assigneeId", "executedById") if k in args}
    defects = dedupe(list(args.get("defectKeys") or []))
    evidence = _evidence(ctx.env, list(args.get("attachments") or []))
    if not (sets_status or entries or assign or steps or defects or evidence):
        raise ToolError("Nothing to update: pass status, steps, fields, rawFields, assigneeId, "
                        "executedById, defectKeys or attachments")

    exec_id = fetch_container(ctx, EXEC, pk, ek)["issueId"]
    test_id = resolve_test_ids(ctx, pk, [tk])[0]
    data = ctx.xray.graphql(_GET_RUN, {"testExecIssueIds": [exec_id], "testIssueIds": [test_id],
                                       "limit": 1})
    runs = ((data.get("getTestRuns") or {}).get("results")) or []
    if not runs:
        raise ToolError("Test %s is not in test execution %s" % (tk, ek), testKey=tk,
                        testExecutionKey=ek)
    run = runs[0]
    run_id = run["id"]
    custom, custom_names = _resolve_run_fields(entries, run, labels)
    run_steps = run.get("steps") or []
    out_of_range = [s["index"] for s in steps if s["index"] >= len(run_steps)]
    if out_of_range:
        raise ToolError("Step index %s out of range: the run has %d step(s)"
                        % (out_of_range, len(run_steps)))
    status = derive_status(run_steps, steps, args.get("status"))

    applied: List[str] = []
    warnings: List[str] = []
    try:
        if custom or assign:
            v: Dict[str, Any] = {"id": run_id}
            v.update(assign)
            if custom:
                v["customFields"] = custom
            r = ctx.xray.graphql(_UPDATE_RUN, v)
            warnings.extend((r.get("updateTestRun") or {}).get("warnings") or [])
            applied.append("run fields")
        for s in steps:
            data = {k: s[k] for k in ("comment", "actualResult") if k in s}
            if s.get("status") is not None:
                data["status"] = normalise_status(s["status"])
            r = ctx.xray.graphql(_UPDATE_STEP, {"testRunId": run_id,
                                                "stepId": run_steps[s["index"]]["id"],
                                                "updateData": data})
            warnings.extend((r.get("updateTestRunStep") or {}).get("warnings") or [])
            applied.append("step %d" % s["index"])
        if defects:
            r = ctx.xray.graphql(_ADD_DEFECTS, {"id": run_id, "issues": defects})
            warnings.extend((r.get("addDefectsToTestRun") or {}).get("warnings") or [])
            applied.append("defects")
        if evidence:
            r = ctx.xray.graphql(_ADD_EVIDENCE, {"id": run_id, "evidence": evidence})
            warnings.extend((r.get("addEvidenceToTestRun") or {}).get("warnings") or [])
            applied.append("evidence")
        if status:
            ctx.xray.graphql(_UPDATE_STATUS, {"id": run_id, "status": status})
    except Exception as e:
        if applied:
            raise with_permission_reason(
                PartialFailure("Test run of %s in %s was partially updated" % (tk, ek),
                               applied=applied, cause=str(e)), e) from None
        raise
    out: Dict[str, Any] = {"testRunId": run_id, "status": status,
                           "customFieldsUpdated": custom_names, "stepsUpdated": len(steps),
                           "defectsLinked": defects, "attachmentsUploaded": len(evidence)}
    out.update(assign)
    if warnings:
        out["warnings"] = warnings
    return out
