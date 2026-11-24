"""Analysis tools: run history and requirement coverage."""
from __future__ import annotations

from typing import Any, Dict, List

from ..context import check_issue_key, require_project
from ..registry import REGISTRY
from ..related import MAX_PAGES, PAGE, latest_statuses_checked, run_on
from .tests import _BASE_JIRA, _GET_TESTS, _LIMIT, _jira_list, _schema, fetch_test

_HISTORY = """
query GetTestRunHistory($testIssueIds: [String], $limit: Int!, $start: Int!) {
  getTestRuns(testIssueIds: $testIssueIds, limit: $limit, start: $start) {
    total
    results {
      status { name }
      startedOn
      finishedOn
      defects
      testExecution { testEnvironments jira(fields: ["key"]) }
    }
  }
}
"""


def _all_runs(ctx, test_id: str) -> List[dict]:
    rows: List[dict] = []
    for page in range(MAX_PAGES):
        data = ctx.xray.graphql(_HISTORY, {"testIssueIds": [test_id], "limit": PAGE,
                                           "start": page * PAGE})
        got = ((data.get("getTestRuns") or {}).get("results")) or []
        rows.extend(got)
        if len(got) < PAGE:
            break
    return rows


@REGISTRY.tool(
    "get_test_runs",
    "Run history of one test, newest first (limit, default 20): execution key, status, start/"
    "finish time and environments; detail adds defect issue ids. The summary gives the total, "
    "counts by status, the last status and time, and flaky (true when both PASSED and FAILED "
    "appear among the returned runs). A test that never ran has lastStatus null. Statuses are "
    "passed through as Xray reports them.",
    _schema({"testKey": {"type": "string"}, "limit": _LIMIT, "detail": {"type": "boolean"}},
            ["testKey"]))
def get_test_runs(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    require_project(ctx, pk, write=False)
    detail = args.get("detail", False)
    test = fetch_test(ctx, pk, key)
    rows = _all_runs(ctx, str(test.get("issueId")))
    rows.sort(key=lambda r: run_on(r) or "", reverse=True)
    rows = rows[:args.get("limit", 20)]
    runs: List[Dict[str, Any]] = []
    by_status: Dict[str, int] = {}
    for r in rows:
        execution = r.get("testExecution") or {}
        status = (r.get("status") or {}).get("name")
        by_status[status or "UNKNOWN"] = by_status.get(status or "UNKNOWN", 0) + 1
        item: Dict[str, Any] = {
            "testExecKey": (execution.get("jira") or {}).get("key"), "status": status,
            "startedOn": r.get("startedOn"), "finishedOn": r.get("finishedOn"),
            "environments": execution.get("testEnvironments") or []}
        if detail:
            item["defects"] = r.get("defects") or []
        runs.append(item)
    last = runs[0] if runs else None
    return {"runs": runs,
            "summary": {"total": len(runs), "byStatus": by_status,
                        "lastStatus": last["status"] if last else None,
                        "lastRunOn": run_on(last) if last else None,
                        "flaky": "PASSED" in by_status and "FAILED" in by_status}}


@REGISTRY.tool(
    "get_requirement_coverage",
    "Tests covering a requirement (a story, epic or other issue of any project, linked with "
    "'is tested by'), each with the status and time of its latest run, plus counts: total, "
    "byLastStatus and neverRun. projectKey is the Xray project the tests live in.",
    _schema({"issueKey": {"type": "string"}}, ["issueKey"]))
def get_requirement_coverage(ctx, args):
    pk, key = args["projectKey"], args["issueKey"]
    require_project(ctx, pk, write=False)
    check_issue_key(key)
    jql = 'issue in linkedIssues("%s", "is tested by")' % key
    data = ctx.xray.graphql(_GET_TESTS % ("", _jira_list(_BASE_JIRA)), {"jql": jql, "limit": 100})
    found = data.get("getTests") or {}
    results = found.get("results") or []
    real_total = found.get("total")
    real_total = real_total if isinstance(real_total, int) else len(results)
    truncated = real_total > len(results)
    latest: Dict[str, Dict[str, Any]] = {}
    if results:
        latest, capped = latest_statuses_checked(
            ctx, [str(r["issueId"]) for r in results if r.get("issueId")])
        truncated = truncated or capped
    tests: List[Dict[str, Any]] = []
    by_status: Dict[str, int] = {}
    never = 0
    for r in results:
        jira = r.get("jira") or {}
        st = latest.get(str(r.get("issueId")))
        if st:
            by_status[st["status"] or "UNKNOWN"] = by_status.get(st["status"] or "UNKNOWN", 0) + 1
        else:
            never += 1
        tests.append({"key": jira.get("key"), "summary": jira.get("summary"),
                      "lastStatus": st["status"] if st else None,
                      "lastRunOn": st["on"] if st else None})
    return {"requirement": key, "tests": tests, "truncated": truncated,
            "counts": {"total": max(real_total, len(tests)), "byLastStatus": by_status, "neverRun": never}}
