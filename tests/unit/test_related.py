from __future__ import annotations

from conftest import make_context
from xray_cloud import related

Q = "LatestTestStatuses"


def run(test_id, status, started=None, finished=None, key="PROJ-90"):
    return {"test": {"issueId": test_id}, "status": {"name": status}, "startedOn": started,
            "finishedOn": finished, "testExecution": {"jira": {"key": key}}}


def ctx_with(handler):
    return make_context("projects:\n  PROJ: {}\n", responses={Q: handler})


def test_latest_status_prefers_latest_finished_then_started():
    runs = [run("1", "FAILED", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "PROJ-1"),
            run("1", "PASSED", "2026-01-05T00:00:00Z", None, "PROJ-2"),  # started only
            run("1", "TODO", "2026-01-03T00:00:00Z", "2026-01-03T01:00:00Z", "PROJ-3")]
    ctx = ctx_with({"getTestRuns": {"total": 3, "results": runs}})
    out = related.latest_statuses(ctx, ["1"])
    assert out == {"1": {"status": "PASSED", "on": "2026-01-05T00:00:00Z",
                         "testExecKey": "PROJ-2"}}


def test_never_run_absent():
    ctx = ctx_with({"getTestRuns": {"total": 1, "results": [run("1", "PASSED", None, "2026-01-01")]}})
    out = related.latest_statuses(ctx, ["1", "2"])
    assert "2" not in out and "1" in out
    assert related.latest_statuses(ctx, []) == {}


def test_chunks_of_50_and_paging_cap():
    seen = []

    def handler(v):
        seen.append((len(v["testIssueIds"]), v["start"], v["limit"]))
        return {"getTestRuns": {"total": 9999, "results": [
            run(v["testIssueIds"][0], "PASSED", None, "2026-01-01")] * 100}}

    ctx = ctx_with(handler)
    related.latest_statuses(ctx, [str(i) for i in range(120)])
    assert sorted({n for n, _, _ in seen}) == [20, 50]
    assert len(seen) == 30  # 3 chunks x 10 pages cap
    assert all(l == 100 for _, _, l in seen)
    assert [s for n, s, _ in seen if n == 20] == [i * 100 for i in range(10)]


def test_short_page_stops_paging():
    seen = []

    def handler(v):
        seen.append(v["start"])
        return {"getTestRuns": {"total": 3, "results": [run("1", "PASSED", None, "2026-01-01")]}}

    related.latest_statuses(ctx_with(handler), ["1"])
    assert seen == [0]


def test_checked_flags_page_cap_and_plain_matches():
    full = {"getTestRuns": {"total": 9999, "results": [run("1", "PASSED", None, "2026-01-01")] * 100}}
    ctx = ctx_with(full)
    out, truncated = related.latest_statuses_checked(ctx, ["1"])
    assert truncated is True and out == related.latest_statuses(ctx, ["1"])
    short = ctx_with({"getTestRuns": {"total": 1, "results": [run("1", "PASSED", None, "2026-01-01")]}})
    assert related.latest_statuses_checked(short, ["1"])[1] is False


def test_untimestamped_run_counts_and_loses_to_timestamped():
    ctx = ctx_with({"getTestRuns": {"total": 3, "results": [
        run("1", "TODO"), run("2", "TODO"), run("2", "PASSED", None, "2026-01-01")]}})
    out = related.latest_statuses(ctx, ["1", "2"])
    assert out["1"]["status"] == "TODO" and out["1"]["on"] is None
    assert out["2"]["status"] == "PASSED"


def test_find_related_always_returns_rows_and_truncated():
    ctx = make_context("projects:\n  PROJ: {}\n", responses={
        "RelatedTests": {"getTests": {"total": 1, "results": [
            {"issueId": "1", "jira": {"key": "PROJ-1", "summary": "a", "labels": []}}]}},
        Q: {"getTestRuns": {"total": 0, "results": []}}})
    out = related.find_related(ctx, "PROJ", related.Criteria(labels=["x"]), 10)
    assert isinstance(out, tuple) and len(out) == 2
    rows, truncated = out
    assert [r["key"] for r in rows] == ["PROJ-1"] and truncated is False
