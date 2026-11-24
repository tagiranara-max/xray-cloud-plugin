from __future__ import annotations

import json

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = "projects:\n  PROJ:{}\n".replace("{}", " {}")


def trun(status, started=None, finished=None, key="PROJ-90", envs=None, defects=None):
    return {"status": {"name": status}, "startedOn": started, "finishedOn": finished,
            "defects": defects or [],
            "testExecution": {"issueId": "900", "testEnvironments": envs or [],
                              "jira": {"key": key}}}


def get_tests(results):
    return {"getTests": {"total": len(results), "results": results}}


def call(ctx, name, **args):
    args.setdefault("projectKey", "PROJ")
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def runs_ctx(runs):
    return make_context(YAML, responses={
        "SearchTests": get_tests([{"issueId": "1", "jira": {"key": "PROJ-1"}}]),
        "GetTestRunHistory": {"getTestRuns": {"total": len(runs), "results": runs}}})


def test_runs_newest_first_and_custom_status_passthrough():
    runs = [trun("TODO", "2026-01-01T00:00:00Z", None, "PROJ-91"),
            trun("BLOCKED", "2026-01-02T00:00:00Z", None, "PROJ-92"),
            trun("EXECUTING", "2026-01-03T00:00:00Z", None, "PROJ-93", envs=["chrome"]),
            trun("PASSED", "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z", "PROJ-94")]
    out, err = call(runs_ctx(runs), "get_test_runs", testKey="PROJ-1")
    assert not err
    assert [r["testExecKey"] for r in out["runs"]] == ["PROJ-94", "PROJ-93",
                                                       "PROJ-92", "PROJ-91"]
    assert out["runs"][1]["environments"] == ["chrome"]
    assert out["runs"][0]["finishedOn"] == "2026-01-04T00:00:00Z"
    assert "defects" not in out["runs"][0]
    s = out["summary"]
    assert s["total"] == 4 and s["lastStatus"] == "PASSED" and s["flaky"] is False
    assert s["byStatus"] == {"PASSED": 1, "EXECUTING": 1, "BLOCKED": 1, "TODO": 1}
    assert s["lastRunOn"] == "2026-01-04T00:00:00Z"


def test_never_ran_last_status_null():
    out, _ = call(runs_ctx([]), "get_test_runs", testKey="PROJ-1")
    assert out["runs"] == []
    assert out["summary"]["lastStatus"] is None and out["summary"]["lastRunOn"] is None
    assert out["summary"]["total"] == 0 and out["summary"]["flaky"] is False


def test_limit_and_detail_defects():
    runs = [trun("PASSED", "2026-01-0%dT00:00:00Z" % i, defects=["77"]) for i in range(1, 6)]
    out, _ = call(runs_ctx(runs), "get_test_runs", testKey="PROJ-1", limit=2, detail=True)
    assert len(out["runs"]) == 2 and out["runs"][0]["defects"] == ["77"]


def test_flaky_only_with_pass_and_fail():
    def flaky(*statuses):
        runs = [trun(s, "2026-01-0%dT00:00:00Z" % (i + 1)) for i, s in enumerate(statuses)]
        return call(runs_ctx(runs), "get_test_runs", testKey="PROJ-1")[0]["summary"]["flaky"]
    assert flaky("PASSED", "FAILED") is True
    assert flaky("PASSED", "PASSED") is False
    assert flaky("FAILED", "BLOCKED", "TODO") is False
    assert flaky("PASSED", "TODO") is False


def cov_ctx(tests, runs):
    return make_context(YAML, responses={
        "SearchTests": get_tests(tests), "LatestTestStatuses": {
            "getTestRuns": {"total": len(runs), "results": runs}}})


def lrun(tid, status, on, key="PROJ-90"):
    return {"test": {"issueId": tid}, "status": {"name": status}, "startedOn": None,
            "finishedOn": on, "testExecution": {"jira": {"key": key}}}


def test_coverage_lists_tests_with_last_status_and_counts():
    tests = [{"issueId": "1", "jira": {"key": "PROJ-1", "summary": "a"}},
             {"issueId": "2", "jira": {"key": "PROJ-2", "summary": "b"}}]
    ctx = cov_ctx(tests, [lrun("1", "PASSED", "2026-01-01"), lrun("2", "FAILED", "2026-01-02"),
                          lrun("2", "PASSED", "2026-01-01")])
    out, err = call(ctx, "get_requirement_coverage", issueKey="OTHER-5")
    assert not err
    assert out["requirement"] == "OTHER-5"
    assert out["tests"] == [
        {"key": "PROJ-1", "summary": "a", "lastStatus": "PASSED", "lastRunOn": "2026-01-01"},
        {"key": "PROJ-2", "summary": "b", "lastStatus": "FAILED", "lastRunOn": "2026-01-02"}]
    assert out["counts"] == {"total": 2, "byLastStatus": {"PASSED": 1, "FAILED": 1}, "neverRun": 0}
    jql = ctx.xray.calls_of("SearchTests")[0]["jql"]
    assert jql == 'issue in linkedIssues("OTHER-5", "is tested by")'


def test_coverage_never_run_counted():
    tests = [{"issueId": "1", "jira": {"key": "PROJ-1", "summary": "a"}},
             {"issueId": "2", "jira": {"key": "PROJ-2", "summary": "b"}}]
    ctx = cov_ctx(tests, [lrun("1", "TODO", "2026-01-01")])
    out, _ = call(ctx, "get_requirement_coverage", issueKey="PROJ-7")
    assert out["tests"][1] == {"key": "PROJ-2", "summary": "b", "lastStatus": None,
                               "lastRunOn": None}
    assert out["counts"] == {"total": 2, "byLastStatus": {"TODO": 1}, "neverRun": 1}


def test_coverage_no_tests_skips_run_query():
    ctx = cov_ctx([], [])
    out, _ = call(ctx, "get_requirement_coverage", issueKey="PROJ-7")
    assert out["counts"] == {"total": 0, "byLastStatus": {}, "neverRun": 0}
    assert ctx.xray.calls_of("LatestTestStatuses") == []


def test_invalid_key_and_unknown_project():
    ctx = cov_ctx([], [])
    assert call(ctx, "get_requirement_coverage", issueKey='X-1") OR (')[1]
    assert call(ctx, "get_requirement_coverage", issueKey="X-1", projectKey="NOPE")[1]


def test_analysis_tools_are_read_tools():
    for n in ("get_test_runs", "get_requirement_coverage"):
        assert REGISTRY.get(n).write is False
