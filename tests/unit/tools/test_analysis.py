from __future__ import annotations

import json

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = "projects:\n  PROJ: {}\n"


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
    assert ctx.xray.calls == []
    assert call(ctx, "get_requirement_coverage", issueKey="X-1", projectKey="NOPE")[1]


def test_analysis_tools_are_read_tools():
    for n in ("get_test_runs", "get_requirement_coverage"):
        assert REGISTRY.get(n).write is False


def test_coverage_truncated_when_tests_cap_hit_and_real_total():
    tests = [{"issueId": "1", "jira": {"key": "PROJ-1", "summary": "a"}}]
    ctx = make_context(YAML, responses={
        "SearchTests": {"getTests": {"total": 250, "results": tests}},
        "LatestTestStatuses": {"getTestRuns": {"total": 0, "results": []}}})
    out, _ = call(ctx, "get_requirement_coverage", issueKey="PROJ-7")
    assert out["truncated"] is True and out["counts"]["total"] == 250


def test_coverage_truncated_when_status_cap_hit():
    tests = [{"issueId": "1", "jira": {"key": "PROJ-1", "summary": "a"}}]
    ctx = make_context(YAML, responses={
        "SearchTests": get_tests(tests),
        "LatestTestStatuses": {"getTestRuns": {"total": 9999,
                                               "results": [lrun("1", "PASSED", "2026-01-01")] * 100}}})
    out, _ = call(ctx, "get_requirement_coverage", issueKey="PROJ-7")
    assert out["truncated"] is True


def test_coverage_not_truncated_normally():
    out, _ = call(cov_ctx([], []), "get_requirement_coverage", issueKey="PROJ-7")
    assert out["truncated"] is False


def test_coverage_untimestamped_run_counts_not_never_run():
    tests = [{"issueId": "1", "jira": {"key": "PROJ-1", "summary": "a"}}]
    ctx = cov_ctx(tests, [lrun("1", "TODO", None)])
    out, _ = call(ctx, "get_requirement_coverage", issueKey="PROJ-7")
    assert out["tests"][0]["lastStatus"] == "TODO" and out["tests"][0]["lastRunOn"] is None
    assert out["counts"]["neverRun"] == 0 and out["counts"]["byLastStatus"] == {"TODO": 1}


# ---- find_related_tests -------------------------------------------------

def rel_row(n, folder=None, labels=None):
    return {"issueId": str(n), "folder": {"path": folder} if folder else None,
            "jira": {"key": "PROJ-%d" % n, "summary": "t%d" % n, "labels": labels or []}}


def rel_ctx(by_jql=None, folder_rows=None, runs=None):
    """by_jql: function(jql) -> rows; folder_rows: rows for the folder query."""
    def tests(v):
        rows = by_jql(v["jql"]) if by_jql else []
        return {"getTests": {"total": len(rows), "results": rows[v["start"]:v["start"] + v["limit"]]}}

    def in_folder(v):
        rows = folder_rows or []
        return {"getTests": {"total": len(rows), "results": rows[v["start"]:v["start"] + v["limit"]]}}
    return make_context(YAML, responses={
        "RelatedTests": tests, "RelatedTestsInFolder": in_folder,
        "GetProjectSettings": {"getProjectSettings": {"projectId": "77"}},
        "LatestTestStatuses": {"getTestRuns": {"total": 0, "results": runs or []}}})


def test_each_kind_builds_its_query():
    from xray_cloud import related
    assert related._jql_for("terms", ['checkout "x]\\y', "refund"], "PROJ") == \
        'project = "PROJ" AND (text ~ "checkout x y" OR text ~ "refund")'
    assert related._jql_for("issueKeys", ["PROJ-1", "STORY-2"], "PROJ") == (
        'project = "PROJ" AND (issue in linkedIssues("PROJ-1", "is tested by") '
        'OR issue in linkedIssues("STORY-2", "is tested by"))')
    assert related._jql_for("labels", ["a", 'b"c'], "PROJ") == \
        'project = "PROJ" AND (labels in ("a", "b\\"c"))'
    assert related._jql_for("components", ["Backend"], "PROJ") == \
        'project = "PROJ" AND (component in ("Backend"))'


def test_bad_issue_key_refused():
    ctx = rel_ctx(lambda j: [])
    out, err = call(ctx, "find_related_tests", issueKeys=['X-1") OR ("a'])
    assert err and not ctx.xray.calls_of("RelatedTests")


def test_merge_dedupes_and_records_matched_by():
    def by(jql):
        if "text ~" in jql:
            return [rel_row(1, "/A", ["x"]), rel_row(2)]
        if "labels in" in jql:
            return [rel_row(1, "/A", ["x"])]
        return []
    ctx = rel_ctx(by)
    out, err = call(ctx, "find_related_tests", terms=["checkout"], labels=["x"])
    assert not err and out["truncated"] is False
    assert [t["key"] for t in out["tests"]] == ["PROJ-1", "PROJ-2"]
    t1 = out["tests"][0]
    assert t1["matchedBy"] == ["terms", "labels"] and t1["folder"] == "/A"
    assert t1["labels"] == ["x"] and t1["lastStatus"] is None
    assert all(c["jql"].startswith('project = "PROJ" AND (')
               for c in ctx.xray.calls_of("RelatedTests"))


def test_rank_by_match_count_then_key_and_last_status():
    def by(jql):
        if "text ~" in jql:
            return [rel_row(5), rel_row(3)]
        return [rel_row(9), rel_row(5)]
    runs = [{"test": {"issueId": "5"}, "status": {"name": "FAILED"}, "startedOn": None,
             "finishedOn": "2026-01-01T00:00:00Z", "testExecution": {"jira": {"key": "PROJ-90"}}}]
    ctx = rel_ctx(by, runs=runs)
    out, _ = call(ctx, "find_related_tests", terms=["a"], labels=["b"])
    assert [t["key"] for t in out["tests"]] == ["PROJ-5", "PROJ-3", "PROJ-9"]
    assert out["tests"][0]["lastStatus"] == "FAILED" and out["tests"][1]["lastStatus"] is None


def test_limit_cap_200_and_truncated_flag():
    big = [rel_row(n) for n in range(1, 501)]
    ctx = rel_ctx(lambda j: big)
    out, err = call(ctx, "find_related_tests", terms=["a"], limit=200)
    assert not err and len(out["tests"]) == 200 and out["truncated"] is True
    # never asks for more than limit rows in total
    asked = sum(c["limit"] for c in ctx.xray.calls_of("RelatedTests"))
    assert asked == 200
    out, err = call(ctx, "find_related_tests", terms=["a"], limit=201)
    assert err
    out, err = call(rel_ctx(lambda j: big[:3]), "find_related_tests", terms=["a"], limit=5)
    assert len(out["tests"]) == 3 and out["truncated"] is False


def test_truncated_when_merged_exceeds_limit():
    def by(jql):
        return [rel_row(1), rel_row(2)] if "text ~" in jql else [rel_row(3), rel_row(4)]
    out, _ = call(rel_ctx(by), "find_related_tests", terms=["a"], labels=["b"], limit=3)
    assert len(out["tests"]) == 3 and out["truncated"] is True


def test_no_criteria_refused():
    ctx = rel_ctx()
    out, err = call(ctx, "find_related_tests")
    assert err and "criterion" in json.dumps(out)
    assert not ctx.xray.calls
    out, err = call(ctx, "find_related_tests", terms=["[]"])
    assert err


def test_folder_criterion_uses_getTests_folder():
    ctx = rel_ctx(folder_rows=[rel_row(1, "/Shop/Returns")])
    out, err = call(ctx, "find_related_tests", folder="Shop/Returns/")
    assert not err
    assert out["tests"][0]["matchedBy"] == ["folder"] and out["tests"][0]["folder"] == "/Shop/Returns"
    v = ctx.xray.calls_of("RelatedTestsInFolder")[0]
    assert v["projectId"] == "77" and v["path"] == "/Shop/Returns"
    assert not ctx.xray.calls_of("RelatedTests")
