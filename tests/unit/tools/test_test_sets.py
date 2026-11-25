from __future__ import annotations

import json

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = """
projects:
  P:
    fields:
      testSet:
        Team: {id: customfield_9, type: option}
      testPlan:
        Release: {id: customfield_8, type: option}
"""


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def members(kind, key="P-10", summary="Suite", issue_id="900", tests=(("P-1", "1"), ("P-2", "2")),
            total=None):
    rows = [{"issueId": i, "testType": {"name": "Manual"}, "jira": {"key": k, "summary": "s " + k}}
            for k, i in tests]
    return {"get%ss" % kind: {"results": [{"issueId": issue_id,
            "jira": {"key": key, "summary": summary},
            "tests": {"total": len(rows) if total is None else total, "results": rows}}]}}


def test_create_set_maps_fields_and_adds_tests():
    ctx = make_context(YAML, responses={
        "GetTestIssueIds": {"getTests": {"results": [
            {"issueId": "1", "jira": {"key": "P-1"}}]}},
        "CreateTestSet": {"createTestSet": {"testSet": {"issueId": "900", "jira": {"key": "P-10"}},
                                            "warnings": []}},
        "AddTestsToTestSet": {"addTestsToTestSet": {"addedTests": ["1"], "warning": None}}})
    res, err = call(ctx, "create_test_set", projectKey="P", summary="Suite", description="d",
                    testKeys=["P-1"], fields={"Team": "A"})
    assert not err and res["key"] == "P-10" and res["addedTests"] == ["P-1"]
    jira = ctx.xray.calls_of("CreateTestSet")[0]["jira"]["fields"]
    assert jira == {"project": {"key": "P"}, "summary": "Suite", "description": "d",
                    "customfield_9": {"value": "A"}}
    assert ctx.xray.calls_of("AddTestsToTestSet")[0] == {"issueId": "900", "testIssueIds": ["1"]}


def test_create_set_rejects_foreign_test_key_before_creating():
    ctx = make_context(YAML)
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", testKeys=["Q-1"])
    assert err and "does not belong" in res["error"]
    assert ctx.xray.calls == []


def test_create_set_new_tests_skip_duplicates_and_create_listed():
    def search(v):
        if "summary ~" in v["jql"] and "Dup" in v["jql"]:
            return {"getTests": {"total": 1, "results": [
                {"issueId": "5", "jira": {"key": "P-5", "summary": "Dup"}}]}}
        return {"getTests": {"total": 0, "results": []}}
    ctx = make_context(YAML, responses={
        "CreateTestSet": {"createTestSet": {"testSet": {"issueId": "900", "jira": {"key": "P-10"}}}},
        "SearchTests": search,
        "CreateTest": lambda v: {"createTest": {"test": {"issueId": "77", "jira": {
            "key": "P-77", "summary": v["jira"]["fields"]["summary"]}}}},
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "77", "jira": {"key": "P-77"}}]}},
        "AddTestsToTestSet": {"addTestsToTestSet": {"addedTests": ["77"], "warning": None}}})
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="",
                    newTests=[{"summary": "Dup"}, {"summary": "Fresh"}])
    assert not err
    assert res["createdTests"] == ["P-77"] and res["addedTests"] == ["P-77"]
    assert res["skippedDuplicates"][0]["index"] == 0
    assert res["skippedDuplicates"][0]["candidates"] == [{"key": "P-5", "summary": "Dup"}]
    assert len(ctx.xray.calls_of("CreateTest")) == 1
    res, _ = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="", createDuplicates=[0],
                  newTests=[{"summary": "Dup"}])
    assert res["createdTests"] == ["P-77"] and "skippedDuplicates" not in res


def test_create_set_new_tests_missing_required_test_field_refused_before_any_write():
    yaml = YAML + "      test:\n        Area: {id: customfield_7, type: option, required: true}\n"
    ctx = make_context(yaml)
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="",
                    newTests=[{"summary": "A"}])
    assert not err and res["status"] == "needs_answer"
    assert res["missingFields"] == [{"name": "Area", "id": "customfield_7"}]
    assert res["questions"][0]["param"] == "testFields"
    assert ctx.xray.calls == []
    # without newTests the test fields are irrelevant
    ctx = make_context(yaml, responses={
        "CreateTestSet": {"createTestSet": {"testSet": {"issueId": "900",
                                                        "jira": {"key": "P-10"}}}}})
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S")
    assert not err, res


def test_create_set_partial_failure_lists_created_keys():
    ctx = make_context(YAML, responses={
        "CreateTestSet": {"createTestSet": {"testSet": {"issueId": "900", "jira": {"key": "P-10"}}}},
        "SearchTests": {"getTests": {"results": []}},
        "CreateTest": lambda v: {"createTest": {"test": {"issueId": "77", "jira": {"key": "P-77"}}}},
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "77", "jira": {"key": "P-77"}}]}}})
    # AddTestsToTestSet has no response -> AssertionError would escape; use a ToolError instead
    from xray_cloud.errors import ToolError

    def boom(v):
        raise ToolError("add failed")
    ctx.xray.responses["AddTestsToTestSet"] = boom
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="",
                    newTests=[{"summary": "A"}])
    assert err and res["created"] == ["P-10", "P-77"]


def test_search_scoped_and_post_filtered():
    ctx = make_context(YAML, responses={"GetTestSets": {"getTestSets": {"results": [
        {"issueId": "1", "jira": {"key": "P-1", "summary": "a"}},
        {"issueId": "2", "jira": {"key": "Q-2", "summary": "b"}}]}}})
    res, err = call(ctx, "search_test_sets", projectKey="P", query="a", jql="x = 1", limit=5)
    c = ctx.xray.calls_of("GetTestSets")[0]
    assert c == {"jql": 'project = P AND summary ~ "a" AND (x = 1)', "limit": 5}
    assert res == {"testSets": [{"key": "P-1", "summary": "a"}]}
    res, err = call(ctx, "search_test_sets", projectKey="P", jql="(x = 1")
    assert err and "unbalanced" in res["error"]
    res, err = call(ctx, "search_test_sets", projectKey="P")
    assert err


def test_get_tests_passes_pagination():
    ctx = make_context(YAML, responses={"GetTestsInTestSet": members("TestSet", total=250)})
    res, _ = call(ctx, "get_tests_in_test_set", projectKey="P", testSetKey="P-10", limit=2, start=4)
    assert ctx.xray.calls_of("GetTestsInTestSet")[0] == {
        "jql": "project = P AND issue = P-10", "testLimit": 2, "testStart": 4}
    assert res["total"] == 250 and res["start"] == 4
    assert res["tests"][0] == {"key": "P-1", "summary": "s P-1", "testType": "Manual"}


def test_delete_previews_then_deletes():
    ctx = make_context(YAML, responses={"GetTestsInTestSet": members("TestSet"),
                                        "DeleteTestSet": {"deleteTestSet": "ok"}})
    res, _ = call(ctx, "delete_test_set", projectKey="P", testSetKey="P-10")
    assert res["wouldDelete"] == {"key": "P-10", "summary": "Suite", "testCount": 2}
    assert ctx.xray.calls_of("DeleteTestSet") == []
    res, _ = call(ctx, "delete_test_set", projectKey="P", testSetKey="P-10", confirm=True)
    assert res["deleted"] is True
    assert ctx.xray.calls_of("DeleteTestSet") == [{"issueId": "900"}]


def test_remove_reports_not_member():
    ctx = make_context(YAML, responses={"GetTestsInTestSet": members("TestSet"),
                                        "RemoveTestsFromTestSet": {"removeTestsFromTestSet": "ok"}})
    res, err = call(ctx, "remove_tests_from_test_set", projectKey="P", testSetKey="P-10",
                    testKeys=["P-1", "P-99"])
    assert not err and res["removed"] == ["P-1"] and res["notMember"] == ["P-99"]
    assert ctx.xray.calls_of("RemoveTestsFromTestSet") == [{"issueId": "900", "testIssueIds": ["1"]}]
    res, _ = call(ctx, "remove_tests_from_test_set", projectKey="P", testSetKey="P-10",
                  testKeys=["P-99"])
    assert res["removed"] == [] and len(ctx.xray.calls_of("RemoveTestsFromTestSet")) == 1


def test_write_tools_blocked_read_only_and_foreign_keys():
    ctx = make_context(YAML, read_only=True)
    res, err = call(ctx, "delete_test_set", projectKey="P", testSetKey="P-10")
    assert err
    ctx = make_context(YAML)
    res, err = call(ctx, "delete_test_set", projectKey="P", testSetKey="Q-10")
    assert err and "does not belong" in res["error"]


def test_test_key_lookup_is_chunked_to_100():
    import re as _re

    def lookup(v):
        keys = _re.findall(r"P-\d+", v["jql"])
        assert len(keys) <= 100 and v["limit"] <= 100
        return {"getTests": {"results": [{"issueId": k[2:], "jira": {"key": k}} for k in keys]}}
    keys = ["P-%d" % i for i in range(1, 251)]
    ctx = make_context(YAML, responses={
        "GetTestIssueIds": lookup,
        "GetTestsInTestSet": members("TestSet", key="P-10", tests=()),
        "AddTestsToTestSet": lambda v: {"addTestsToTestSet": {"addedTests": v["testIssueIds"]}}})
    res, err = call(ctx, "add_tests_to_test_set", projectKey="P", testSetKey="P-10",
                    testKeys=keys)
    assert not err, res
    assert [c["limit"] for c in ctx.xray.calls_of("GetTestIssueIds")] == [100, 100, 50]
    assert res["added"] == keys


# ---- AI label choice (newTests only)

def _set_responses():
    return {
        "CreateTestSet": {"createTestSet": {"testSet": {"issueId": "900", "jira": {"key": "P-10"}}}},
        "SearchTests": {"getTests": {"total": 0, "results": []}},
        "CreateTest": lambda v: {"createTest": {"test": {"issueId": "77", "jira": {
            "key": "P-77", "summary": v["jira"]["fields"]["summary"]}}}},
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "77", "jira": {"key": "P-77"}}]}},
        "AddTestsToTestSet": {"addTestsToTestSet": {"addedTests": ["77"], "warning": None}}}


def test_create_set_new_tests_unanswered_ai_label_asks_with_zero_calls():
    ctx = make_context(YAML, responses=_set_responses())
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S",
                    newTests=[{"summary": "A"}])
    assert not err and res["status"] == "needs_answer"
    assert [q["id"] for q in res["questions"]] == ["aiLabel"]
    assert ctx.xray.calls == []


def test_create_set_new_tests_get_label_remembered_next_call_asks_nothing():
    ctx = make_context(YAML, responses=_set_responses())
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="bot",
                    newTests=[{"summary": "A"}])
    assert not err and res["createdTests"] == ["P-77"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["bot"]
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S",
                    newTests=[{"summary": "B"}])
    assert not err and res["createdTests"] == ["P-77"]
    assert ctx.xray.calls_of("CreateTest")[1]["jira"]["fields"]["labels"] == ["bot"]


def test_create_set_without_new_tests_never_asks_and_ignores_ai_label():
    ctx = make_context(YAML, responses=_set_responses())
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S")
    assert not err and res["created"]
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="bad label")
    assert not err and res["created"]
    # ignored: nothing remembered, so a later newTests call still asks
    res, _ = call(ctx, "create_test_set", projectKey="P", summary="S", newTests=[{"summary": "A"}])
    assert res["status"] == "needs_answer"


def test_create_set_whitespace_ai_label_with_new_tests_refused_before_any_call():
    ctx = make_context(YAML)
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel=" ",
                    newTests=[{"summary": "A"}])
    assert err and "aiLabel" in res["error"]
    assert ctx.xray.calls == []


def _prefs(ctx):
    return ctx.preferences.get("global", "P")


def test_create_set_refused_call_remembers_nothing():
    yaml = YAML + "      test:\n        Area: {id: customfield_7, type: option, required: true}\n"
    ctx = make_context(yaml)
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="",
                    newTests=[{"summary": "A"}])
    assert res["status"] == "needs_answer" and _prefs(ctx) == {}
    ctx = make_context(YAML)
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="bot",
                    createDuplicates=[5], newTests=[{"summary": "A"}])
    assert err and _prefs(ctx) == {}


def test_create_set_settings_label_is_used_but_not_remembered():
    ctx = make_context("aiCreated: {label: team}\n" + YAML, responses=_set_responses())
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S",
                    newTests=[{"summary": "A"}])
    assert not err and res["createdTests"] == ["P-77"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["team"]
    assert _prefs(ctx) == {}


def test_create_set_remembered_label_is_not_rewritten_and_empty_label_adds_none():
    ctx = make_context(YAML, responses=_set_responses())
    ctx.preferences.update("global", "P", {"aiLabel": "old"})
    call(ctx, "create_test_set", projectKey="P", summary="S", newTests=[{"summary": "A"}])
    assert _prefs(ctx) == {"aiLabel": "old"}
    ctx = make_context(YAML, responses=_set_responses())
    ctx.env["XRAY_REGION"] = "eu"
    res, err = call(ctx, "create_test_set", projectKey="P", summary="S", aiLabel="",
                    newTests=[{"summary": "A"}])
    assert not err and "labels" not in ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]
    assert ctx.preferences.get("eu", "P") == {"aiLabel": ""}
