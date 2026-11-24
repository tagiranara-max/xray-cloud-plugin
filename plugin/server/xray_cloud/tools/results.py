"""Import test execution results (JUnit, TestNG, NUnit, Robot, xray JSON, Cucumber, Behave)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..context import check_issue_in_project, require_project
from ..errors import ToolError
from ..fields import build_jira_fields
from .. import localfiles
from ..registry import REGISTRY
from .tests import _OBJ, _schema

XML_FORMATS = ("junit", "testng", "nunit", "robot")
JSON_FORMATS = ("xray", "cucumber", "behave")
FORMATS = JSON_FORMATS + XML_FORMATS
PAGE = 100

_RUNS = """
query GetTestExecutionRuns($testExecIssueIds: [String], $limit: Int!, $start: Int!) {
  getTestRuns(testExecIssueIds: $testExecIssueIds, limit: $limit, start: $start) {
    total
    results { status { name } }
  }
}
"""


def _endpoint(fmt: str, multipart: bool) -> str:
    base = "import/execution" if fmt == "xray" else "import/execution/" + fmt
    return base + "/multipart" if multipart else base


def _read_input(ctx, args: dict) -> bytes:
    has_file, has_content = args.get("filePath") is not None, args.get("content") is not None
    if has_file == has_content:
        raise ToolError("Provide exactly one of filePath or content",
                        filePath=has_file, content=has_content)
    if has_file:
        return localfiles.read_local_file(ctx.env, args["filePath"], "Results file")[1]
    data = args["content"].encode("utf-8")
    if len(data) > localfiles.MAX_FILE_BYTES:
        raise ToolError("Results content is too large: %.1f MB; the limit is %d MB"
                        % (len(data) / (1024 * 1024), localfiles.MAX_FILE_BYTES // (1024 * 1024)),
                        maxBytes=localfiles.MAX_FILE_BYTES)
    return data


def _status_summary(ctx, issue_id: str) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    total = start = 0
    while True:
        runs = (ctx.xray.graphql(_RUNS, {"testExecIssueIds": [issue_id], "limit": PAGE,
                                         "start": start}).get("getTestRuns")) or {}
        rows = runs.get("results") or []
        for r in rows:
            name = (r.get("status") or {}).get("name") or "UNKNOWN"
            by_status[name] = by_status.get(name, 0) + 1
        total += len(rows)
        start += len(rows)
        if not rows or start >= (runs.get("total") or 0):
            return {"total": total, "byStatus": by_status}


@REGISTRY.tool(
    "import_execution_results",
    "Import test results into Xray. format: xray | cucumber | behave (JSON) or junit | testng | "
    "nunit | robot (XML). Give exactly one of filePath (sent byte for byte, max 25 MB) or content. "
    "Without testExecKey a new Test Execution is created (summary/fields/rawFields and the "
    "project's testExecution field settings apply; required fields are checked before upload); "
    "with testExecKey results go to that execution. testPlanKey and testEnvironments are "
    "supported for XML formats, or for JSON formats when a new execution is created with fields. "
    "Returns the execution key and a count of its runs by status; detail adds the raw response.",
    _schema({"format": {"type": "string", "enum": list(FORMATS)},
             "filePath": {"type": "string"}, "content": {"type": "string"},
             "testExecKey": {"type": "string"}, "testPlanKey": {"type": "string"},
             "testEnvironments": {"type": "array", "items": {"type": "string"}},
             "summary": {"type": "string"}, "fields": _OBJ, "rawFields": _OBJ,
             "detail": {"type": "boolean"}}, ["format"]),
    write=True)
def import_execution_results(ctx, args):
    pk, fmt = args["projectKey"], args["format"]
    project = require_project(ctx, pk, write=True)
    if fmt not in FORMATS:
        raise ToolError("Unknown format '%s'" % fmt, formats=list(FORMATS))
    exec_key, plan_key = args.get("testExecKey"), args.get("testPlanKey")
    envs = [e for e in (args.get("testEnvironments") or [])]
    for k in (exec_key, plan_key):
        if k:
            check_issue_in_project(k, pk)
    wants_fields = any(args.get(n) for n in ("summary", "fields", "rawFields"))
    if exec_key and wants_fields:
        raise ToolError("summary, fields and rawFields only apply when a new execution is "
                        "created; omit them or omit testExecKey")
    data = _read_input(ctx, args)
    # Decide the endpoint and validate everything before the first Xray call.
    multipart = False
    info: Optional[dict] = None
    if not exec_key and (wants_fields or (project.fields.get("testExecution") or {})):
        extra = build_jira_fields(project, "testExecution", args.get("fields"),
                                  args.get("rawFields"))
        multipart = True
        info = {"fields": {"project": {"key": pk},
                           "summary": args.get("summary") or "Imported results (%s)" % fmt,
                           "issuetype": {"name": "Test Execution"}, **extra}}
        xf: Dict[str, Any] = {}
        if plan_key:
            xf["testPlanKey"] = plan_key
        if envs:
            xf["environments"] = envs
        if xf:
            info["xrayFields"] = xf
    elif fmt in JSON_FORMATS and (exec_key or plan_key or envs):
        raise ToolError("For %s results testExecKey, testPlanKey and testEnvironments cannot be "
                        "sent with the plain endpoint; put them in the report itself or let a new "
                        "execution be created with summary/fields" % fmt)
    ctype = "text/xml" if fmt in XML_FORMATS else "application/json"
    path = _endpoint(fmt, multipart)
    if multipart:
        part = "result" if fmt in ("cucumber", "behave") else "results"
        resp = ctx.xray.rest("POST", path, multipart={"fields": {}, "files": {
            "info": ("info.json", json.dumps(info).encode("utf-8"), "application/json"),
            part: ("results", data, ctype)}})
    else:
        params: Dict[str, str] = {}
        if fmt in XML_FORMATS:
            params["projectKey"] = pk
            if exec_key:
                params["testExecKey"] = exec_key
            if plan_key:
                params["testPlanKey"] = plan_key
            if envs:
                params["testEnvironments"] = ";".join(envs)
        kw: Dict[str, Any] = {"data": data, "content_type": ctype}
        if params:
            kw["params"] = params
        resp = ctx.xray.rest("POST", path, **kw)
    issue = (resp.get("testExecIssue") if isinstance(resp, dict) else None) or {}
    key = issue.get("key") or exec_key
    out: Dict[str, Any] = {"testExecKey": key, "created": not exec_key}
    warnings: List[str] = []
    issue_id = issue.get("id")
    try:
        if not issue_id:
            raise ToolError("Xray did not return the test execution id")
        out["summary"] = _status_summary(ctx, str(issue_id))
    except Exception as e:
        out["summary"] = None
        warnings.append("Results were imported but the run summary could not be read: %s" % e)
    if warnings:
        out["warnings"] = warnings
    if args.get("detail"):
        out["response"] = resp
    return out
