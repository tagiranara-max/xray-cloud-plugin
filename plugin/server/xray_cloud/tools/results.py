"""Import test execution results (JUnit, TestNG, NUnit, Robot, xray JSON, Cucumber, Behave)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

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


def _parse_json(data: bytes, fmt: str) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ToolError("The %s results are not valid UTF-8 JSON: %s" % (fmt, e)) from None


def _with_target(report: Any, exec_key: Optional[str], plan_key: Optional[str],
                 envs: List[str]) -> bytes:
    """An Xray JSON report aimed at an existing execution / plan / environments."""
    if not isinstance(report, dict):
        raise ToolError("Xray JSON results must be an object with a tests list")
    report = dict(report)
    if exec_key:
        have = report.get("testExecutionKey")
        if have and have != exec_key:
            raise ToolError("The report names testExecutionKey %s, not %s" % (have, exec_key))
        report["testExecutionKey"] = exec_key
    if plan_key or envs:
        info = dict(report.get("info") or {})
        for name, value in (("testPlanKey", plan_key), ("testEnvironments", envs or None)):
            if value is None:
                continue
            if info.get(name) not in (None, "", [], value):
                raise ToolError("The report's info.%s differs from the %s argument"
                                % (name, name), report=info.get(name), argument=value)
            info[name] = value
        report["info"] = info
    return json.dumps(report).encode("utf-8")


_TEST_TAG = re.compile(r"^@TEST_([A-Z][A-Z0-9_]*-[1-9][0-9]*)$")
_FAILED = {"failed", "undefined", "ambiguous"}
# cucumber-jvm / cucumber-js ids of Scenario Outline example rows end in ";<examples>;<row>"
_EXAMPLE_ROW_ID = re.compile(r";[^;]*;\d+$")

Result = Tuple[str, str, str]  # (status, label, error message)


def _results(element: dict) -> List[Result]:
    """Every result of one Cucumber JSON element in run order: scenario hooks, and each step
    with its own before/after hooks (as cucumber-jvm emits them)."""
    out: List[Result] = []

    def add(item: Any, label: str) -> None:
        res = (item.get("result") or {}) if isinstance(item, dict) else {}
        out.append((str(res.get("status") or "").lower(), label,
                    str(res.get("error_message") or "")))

    for hook in element.get("before") or []:
        add(hook, "Before hook")
    for st in element.get("steps") or []:
        if not isinstance(st, dict):
            continue
        name = ("%s%s" % (st.get("keyword") or "", st.get("name") or "")).strip() or "step"
        for hook in st.get("before") or []:
            add(hook, "Before step hook (%s)" % name)
        add(st, name)
        for hook in st.get("after") or []:
            add(hook, "After step hook (%s)" % name)
    for hook in element.get("after") or []:
        add(hook, "After hook")
    return out


def _status(results: List[Result], has_steps: bool) -> Tuple[str, Optional[str]]:
    """(Xray status, first failure): FAILED when a step or hook failed (or is undefined/
    ambiguous), PASSED when there are steps and everything passed, else TODO."""
    for status, label, error in results:
        if status in _FAILED:
            return "FAILED", ("%s: %s" % (label, error or status))[:2000]
    if has_steps and all(st == "passed" for st, _, _ in results):
        return "PASSED", None
    return "TODO", None


def _is_background(el: dict) -> bool:
    return el.get("type") == "background" or \
        str(el.get("keyword") or "").strip().lower() == "background"


def _is_outline_row(el: dict) -> bool:
    keyword = str(el.get("keyword") or "").strip().lower()
    return keyword in ("scenario outline", "scenario template") or \
        bool(_EXAMPLE_ROW_ID.search(str(el.get("id") or "")))


_RANK = {"FAILED": 2, "TODO": 1, "PASSED": 0}


def cucumber_to_xray(report: Any, pk: str) -> dict:
    """Convert a Cucumber JSON report to Xray JSON (tests by @TEST_<KEY> tag). Every scenario
    must carry exactly one @TEST_ key of the project; otherwise nothing is sent. A Background
    element's results count for the scenario that follows it. Scenario Outline rows of one key
    give one example status per row; other repeats of a key keep the worst status."""
    if not isinstance(report, list):
        raise ToolError("Cucumber JSON results must be a list of features")
    # key -> [(status, failure, is outline row)]
    by_key: Dict[str, List[Tuple[str, Optional[str], bool]]] = {}
    untagged: List[str] = []
    for feature in report:
        if not isinstance(feature, dict):
            raise ToolError("Cucumber JSON results must be a list of feature objects")
        background: List[Result] = []
        for el in feature.get("elements") or []:
            if not isinstance(el, dict):
                continue
            if _is_background(el):
                background = _results(el)
                continue
            carried, background = background, []
            keys = sorted({m.group(1) for m in (_TEST_TAG.match(str((t or {}).get("name") or ""))
                                                for t in el.get("tags") or []) if m})
            where = "%s / %s" % (feature.get("name") or feature.get("uri") or "?",
                                 el.get("name") or "?")
            if len(keys) != 1:
                untagged.append(where)
                continue
            check_issue_in_project(keys[0], pk)
            status, failure = _status(carried + _results(el), bool(el.get("steps")))
            by_key.setdefault(keys[0], []).append((status, failure, _is_outline_row(el)))
    if untagged:
        raise ToolError("Cucumber results can go into an existing execution only when every "
                        "scenario is tagged with one @TEST_<KEY> (Xray Cloud's Cucumber import "
                        "always creates a new execution, so the results are sent as Xray JSON). "
                        "Tag these scenarios or omit testExecKey: %s" % "; ".join(untagged[:20]),
                        scenarios=untagged)
    if not by_key:
        raise ToolError("The Cucumber report has no scenarios")
    tests: List[Dict[str, Any]] = []
    for key, rows in by_key.items():
        statuses = [st for st, _, _ in rows]
        item: Dict[str, Any] = {"testKey": key,
                                "status": max(statuses, key=lambda st: _RANK[st])}
        comments = [c for _, c, _ in rows if c]
        if comments:
            item["comment"] = "\n".join(comments)[:4000]
        if len(rows) > 1 and all(outline for _, _, outline in rows):
            item["examples"] = statuses
        tests.append(item)
    return {"tests": tests}


def _exec_info(pk: str, summary: str, extra: Dict[str, Any], plan_key: Optional[str],
               envs: List[str]) -> dict:
    """The multipart `info` part: Jira fields of the new Test Execution plus xrayFields."""
    info: Dict[str, Any] = {"fields": {"project": {"key": pk}, "summary": summary,
                                       "issuetype": {"name": "Test Execution"}, **extra}}
    xf: Dict[str, Any] = {}
    if plan_key:
        xf["testPlanKey"] = plan_key
    if envs:
        xf["environments"] = envs
    if xf:
        info["xrayFields"] = xf
    return info


@REGISTRY.tool(
    "import_execution_results",
    "Import test results into Xray. format: xray | cucumber | behave (JSON) or junit | testng | "
    "nunit | robot (XML). Give exactly one of filePath (sent byte for byte, max 25 MB) or content. "
    "Without testExecKey a new Test Execution is created (summary/fields/rawFields and the "
    "project's testExecution field settings apply; required fields are checked before upload); "
    "with testExecKey results go to that execution (cucumber: every scenario needs a @TEST_<KEY> "
    "tag, and the results are sent as Xray JSON with one status per test; behave: not "
    "supported, Xray always creates a new execution). testPlanKey and testEnvironments work "
    "with every format. "
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
    sent_as = fmt
    if not exec_key and (wants_fields or (project.fields.get("testExecution") or {})):
        extra = build_jira_fields(project, "testExecution", args.get("fields"),
                                  args.get("rawFields"))
        multipart = True
        info = _exec_info(pk, args.get("summary") or "Imported results (%s)" % fmt, extra,
                          plan_key, envs)
    elif fmt == "behave" and exec_key:
        raise ToolError("Behave results cannot go into an existing execution: Xray Cloud's "
                        "Behave endpoints always create a new Test Execution. Omit testExecKey.")
    elif fmt == "cucumber" and exec_key:
        # Xray Cloud's Cucumber endpoints always create a new Test Execution (the multipart
        # info rejects xrayFields.testExecKey; a testExecKey query parameter is ignored), so
        # the report is converted to Xray JSON, whose testExecutionKey targets the execution.
        report = cucumber_to_xray(_parse_json(data, fmt), pk)
        sent_as = "xray"
        data = _with_target(report, exec_key, plan_key, envs)
    elif fmt == "xray" and (exec_key or plan_key or envs):
        sent_as = "xray"
        data = _with_target(_parse_json(data, fmt), exec_key, plan_key, envs)
    elif fmt in JSON_FORMATS and (plan_key or envs):
        # The plain Cucumber/Behave endpoints take no parameters: a new execution through
        # the multipart endpoint carries them in info.xrayFields.
        multipart = True
        info = _exec_info(pk, "Imported results (%s)" % fmt, {}, plan_key, envs)
    ctype = "text/xml" if fmt in XML_FORMATS else "application/json"
    path = _endpoint(sent_as, multipart)
    if multipart:
        # Verified live: every format's multipart part is "results" (Cucumber with "result"
        # gets HTTP 400 "Unexpected field (result)").
        resp = ctx.xray.rest("POST", path, multipart={"fields": {}, "files": {
            "info": ("info.json", json.dumps(info).encode("utf-8"), "application/json"),
            "results": ("results", data, ctype)}})
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
    # Verified live (plain and multipart): Xray Cloud answers {"id", "key", "self"} of the
    # Test Execution; "testExecIssue" (Xray Server/DC shape) is accepted too.
    resp_d = resp if isinstance(resp, dict) else {}
    issue = resp_d.get("testExecIssue") if isinstance(resp_d.get("testExecIssue"), dict) \
        else resp_d
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
    if sent_as != fmt:
        out["sentAs"] = ("xray JSON converted from cucumber (one status per @TEST_ key, "
                         "failures as comment)")
    if args.get("detail"):
        out["response"] = resp
    return out
