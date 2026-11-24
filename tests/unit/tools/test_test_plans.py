from __future__ import annotations

import json

from conftest import make_context
from test_test_sets import YAML, members
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def test_create_plan_maps_fields_and_expands_test_sets():
    ctx = make_context(YAML, responses={
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "1", "jira": {"key": "P-1"}}]}},
        "GetTestsInTestSet": members("TestSet", key="P-20", tests=(("P-1", "1"), ("P-2", "2"))),
        "CreateTestPlan": {"createTestPlan": {"testPlan": {"issueId": "800",
                                                           "jira": {"key": "P-30"}}}},
        "AddTestsToTestPlan": {"addTestsToTestPlan": {"addedTests": ["1", "2"], "warning": "w"}}})
    res, err = call(ctx, "create_test_plan", projectKey="P", summary="Plan", testKeys=["P-1"],
                    testSetKeys=["P-20"], fields={"Release": "R1"})
    assert not err and res["key"] == "P-30" and res["warnings"] == ["w"]
    fields = ctx.xray.calls_of("CreateTestPlan")[0]["jira"]["fields"]
    assert fields["customfield_8"] == {"value": "R1"} and "customfield_9" not in fields
    assert ctx.xray.calls_of("AddTestsToTestPlan")[0] == {"issueId": "800",
                                                          "testIssueIds": ["1", "2"]}


def test_add_requires_something_and_checks_set_keys():
    ctx = make_context(YAML)
    res, err = call(ctx, "add_tests_to_test_plan", projectKey="P", testPlanKey="P-30")
    assert err and "at least one" in res["error"]
    res, err = call(ctx, "add_tests_to_test_plan", projectKey="P", testPlanKey="P-30",
                    testSetKeys=["Q-1"])
    assert err and "does not belong" in res["error"]


def test_add_via_test_set():
    ctx = make_context(YAML, responses={
        "GetTestsInTestSet": members("TestSet", key="P-20"),
        "GetTestsInTestPlan": members("TestPlan", key="P-30", issue_id="800", tests=()),
        "AddTestsToTestPlan": {"addTestsToTestPlan": {"addedTests": ["1", "2"], "warning": None}}})
    res, err = call(ctx, "add_tests_to_test_plan", projectKey="P", testPlanKey="P-30",
                    testSetKeys=["P-20"])
    assert not err and res["added"] == ["P-1", "P-2"]


def test_remove_via_test_set_reports_not_member():
    ctx = make_context(YAML, responses={
        "GetTestsInTestSet": members("TestSet", key="P-20"),
        "GetTestsInTestPlan": members("TestPlan", key="P-30", issue_id="800",
                                      tests=(("P-1", "1"),)),
        "RemoveTestsFromTestPlan": {"removeTestsFromTestPlan": "ok"}})
    res, _ = call(ctx, "remove_tests_from_test_plan", projectKey="P", testPlanKey="P-30",
                  testSetKeys=["P-20"])
    assert res["removed"] == ["P-1"] and res["notMember"] == ["P-2"]


def test_search_get_delete():
    ctx = make_context(YAML, responses={
        "GetTestPlans": {"getTestPlans": {"results": [
            {"issueId": "1", "jira": {"key": "P-30", "summary": "Plan"}}]}},
        "GetTestsInTestPlan": members("TestPlan", key="P-30", issue_id="800"),
        "DeleteTestPlan": {"deleteTestPlan": "ok"}})
    res, _ = call(ctx, "search_test_plans", projectKey="P", query="Plan")
    assert res == {"testPlans": [{"key": "P-30", "summary": "Plan"}]}
    res, _ = call(ctx, "get_tests_in_test_plan", projectKey="P", testPlanKey="P-30", limit=1, start=1)
    assert ctx.xray.calls_of("GetTestsInTestPlan")[0]["testStart"] == 1
    res, _ = call(ctx, "delete_test_plan", projectKey="P", testPlanKey="P-30")
    assert res["wouldDelete"]["testCount"] == 2
    res, _ = call(ctx, "delete_test_plan", projectKey="P", testPlanKey="P-30", confirm=True)
    assert res["deleted"] and ctx.xray.calls_of("DeleteTestPlan") == [{"issueId": "800"}]


def test_create_plan_new_tests_ai_label_asks_then_labels():
    ctx = make_context(YAML, responses={
        "CreateTestPlan": {"createTestPlan": {"testPlan": {"issueId": "800",
                                                           "jira": {"key": "P-30"}}}},
        "SearchTests": {"getTests": {"total": 0, "results": []}},
        "CreateTest": {"createTest": {"test": {"issueId": "77", "jira": {"key": "P-77"}}}},
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "77", "jira": {"key": "P-77"}}]}},
        "AddTestsToTestPlan": {"addTestsToTestPlan": {"addedTests": ["77"], "warning": None}}})
    res, err = call(ctx, "create_test_plan", projectKey="P", summary="Plan",
                    newTests=[{"summary": "A"}])
    assert not err and res["status"] == "needs_answer" and ctx.xray.calls == []
    res, err = call(ctx, "create_test_plan", projectKey="P", summary="Plan", aiLabel="bot",
                    newTests=[{"summary": "A"}])
    assert not err and res["createdTests"] == ["P-77"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["bot"]


def test_create_plan_without_new_tests_asks_nothing():
    ctx = make_context(YAML, responses={
        "CreateTestPlan": {"createTestPlan": {"testPlan": {"issueId": "800",
                                                           "jira": {"key": "P-30"}}}}})
    res, err = call(ctx, "create_test_plan", projectKey="P", summary="Plan")
    assert not err and res["created"]
