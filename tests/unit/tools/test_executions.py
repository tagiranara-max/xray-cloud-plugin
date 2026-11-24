from __future__ import annotations

import base64
import json

import pytest

from conftest import make_context
from test_test_sets import members
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = """
projects:
  P:
    fields:
      testExecution:
        fixVersion: {id: fixVersions, type: versions, required: true}
      testRun:
        version: {name: Version}
        build: {id: cf_build}
"""


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def exec_members(key="P-50", issue_id="500", tests=(("P-1", "1"), ("P-2", "2"))):
    return members("TestExecution", key=key, summary="Run 1", issue_id=issue_id, tests=tests)


def run(steps=3, custom=(("cf-v", "Version"), ("cf-o", "Other"))):
    return {"getTestRuns": {"results": [{
        "id": "run-1",
        "customFields": [{"id": i, "name": n} for i, n in custom],
        "steps": [{"id": "s%d" % i, "status": {"name": "TODO"}} for i in range(steps)]}]}}


def run_ctx(tmp_path=None, runs=None, **extra):
    responses = {
        "GetTestsInTestExecution": exec_members(),
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "1", "jira": {"key": "P-1"}}]}},
        "GetTestRuns": runs if runs is not None else run(),
        "UpdateTestRun": {"updateTestRun": {"warnings": []}},
        "UpdateTestRunStep": {"updateTestRunStep": {"warnings": []}},
        "UpdateTestRunStatus": {"updateTestRunStatus": "ok"},
        "AddDefectsToTestRun": {"addDefectsToTestRun": {"addedDefects": ["P-9"], "warnings": []}},
        "AddEvidenceToTestRun": {"addEvidenceToTestRun": {"addedEvidence": ["e"],
                                                          "warnings": []}},
    }
    responses.update(extra)
    return make_context(YAML, responses=responses, tmp_path=tmp_path)


def ops(ctx):
    return [o for o, _ in ctx.xray.calls]


# ---- create

def test_execution_required_field_missing_refused_before_call():
    ctx = make_context(YAML)
    res, err = call(ctx, "create_test_execution", projectKey="P", summary="Run")
    assert err and res["error"] == "Missing required fields"
    assert res["missingFields"] == [{"name": "fixVersion", "id": "fixVersions"}]
    assert ctx.xray.calls == []


def test_create_execution_with_plan_adds_plan_tests_and_links():
    ctx = make_context(YAML, responses={
        "GetTestsInTestPlan": members("TestPlan", key="P-30", issue_id="800"),
        "CreateTestExecution": {"createTestExecution": {
            "testExecution": {"issueId": "500", "jira": {"key": "P-50"}}, "warnings": ["w"]}},
        "AddTestsToTestExecution": {"addTestsToTestExecution": {"addedTests": ["1", "2"],
                                                                "warning": None}},
        "AddTestExecutionsToTestPlan": {"addTestExecutionsToTestPlan": {
            "addedTestExecutions": ["500"], "warning": None}}})
    res, err = call(ctx, "create_test_execution", projectKey="P", summary="Run",
                    environments=["staging"], testPlanKey="P-30", fields={"fixVersion": "1.0"})
    assert not err and res["key"] == "P-50" and res["testPlanKey"] == "P-30"
    assert res["addedTests"] == ["P-1", "P-2"]
    create = ctx.xray.calls_of("CreateTestExecution")[0]
    assert create["testEnvironments"] == ["staging"]
    assert create["jira"]["fields"]["fixVersions"] == [{"name": "1.0"}]
    assert ctx.xray.calls_of("AddTestsToTestExecution")[0] == {"issueId": "500",
                                                               "testIssueIds": ["1", "2"]}
    assert ctx.xray.calls_of("AddTestExecutionsToTestPlan")[0] == {"issueId": "800",
                                                                   "testExecIssueIds": ["500"]}
    assert ops(ctx).index("GetTestsInTestPlan") < ops(ctx).index("CreateTestExecution")


def test_create_execution_explicit_tests_override_plan_tests_and_link_failure_is_partial():
    def link(_):
        raise RuntimeError("boom")
    ctx = make_context(YAML, responses={
        "GetTestsInTestPlan": members("TestPlan", key="P-30", issue_id="800"),
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "7", "jira": {"key": "P-7"}}]}},
        "CreateTestExecution": {"createTestExecution": {
            "testExecution": {"issueId": "500", "jira": {"key": "P-50"}}, "warnings": []}},
        "AddTestsToTestExecution": {"addTestsToTestExecution": {"addedTests": ["7"],
                                                                "warning": None}},
        "AddTestExecutionsToTestPlan": link})
    res, err = call(ctx, "create_test_execution", projectKey="P", summary="Run",
                    testPlanKey="P-30", testKeys=["P-7"], fields={"fixVersion": "1.0"})
    assert err and res["created"] == ["P-50"]
    assert ctx.xray.calls_of("AddTestsToTestExecution")[0]["testIssueIds"] == ["7"]
    assert ctx.xray.calls_of("CreateTestExecution")[0]["testEnvironments"] is None


def test_create_execution_checks_keys():
    ctx = make_context(YAML)
    res, err = call(ctx, "create_test_execution", projectKey="P", summary="Run",
                    testPlanKey="Q-1", fields={"fixVersion": "1"})
    assert err and "does not belong" in res["error"] and ctx.xray.calls == []


# ---- membership

def test_add_tests_from_plan_without_linking():
    ctx = make_context(YAML, responses={
        "GetTestsInTestExecution": exec_members(tests=()),
        "GetTestsInTestPlan": members("TestPlan", key="P-30", issue_id="800"),
        "GetTestsInTestSet": members("TestSet", key="P-20", tests=(("P-2", "2"), ("P-3", "3"))),
        "AddTestsToTestExecution": {"addTestsToTestExecution": {"addedTests": ["1", "2", "3"],
                                                                "warning": "w"}}})
    res, err = call(ctx, "add_tests_to_test_execution", projectKey="P", testExecutionKey="P-50",
                    testPlanKey="P-30", testSetKeys=["P-20"])
    assert not err and res["added"] == ["P-1", "P-2", "P-3"] and res["warnings"] == ["w"]
    assert ctx.xray.calls_of("AddTestsToTestExecution")[0]["testIssueIds"] == ["2", "3", "1"]
    assert "AddTestExecutionsToTestPlan" not in ops(ctx)


def test_add_tests_requires_something():
    ctx = make_context(YAML)
    res, err = call(ctx, "add_tests_to_test_execution", projectKey="P", testExecutionKey="P-50")
    assert err and "at least one" in res["error"]


def test_remove_tests_reports_not_member():
    ctx = make_context(YAML, responses={
        "GetTestsInTestExecution": exec_members(tests=(("P-1", "1"),)),
        "RemoveTestsFromTestExecution": {"removeTestsFromTestExecution": "ok"}})
    res, err = call(ctx, "remove_tests_from_test_execution", projectKey="P",
                    testExecutionKey="P-50", testKeys=["P-1", "P-2"])
    assert not err and res["removed"] == ["P-1"] and res["notMember"] == ["P-2"]
    assert ctx.xray.calls_of("RemoveTestsFromTestExecution")[0] == {"issueId": "500",
                                                                    "testIssueIds": ["1"]}


def test_add_executions_to_plan():
    ctx = make_context(YAML, responses={
        "GetTestsInTestPlan": members("TestPlan", key="P-30", issue_id="800"),
        "GetTestsInTestExecution": exec_members(),
        "AddTestExecutionsToTestPlan": {"addTestExecutionsToTestPlan": {
            "addedTestExecutions": ["500"], "warning": None}}})
    res, err = call(ctx, "add_test_executions_to_test_plan", projectKey="P", testPlanKey="P-30",
                    testExecutionKeys=["P-50"])
    assert not err and res["addedTestExecutions"] == ["P-50"]
    res, err = call(ctx, "add_test_executions_to_test_plan", projectKey="P", testPlanKey="P-30",
                    testExecutionKeys=["Q-50"])
    assert err and "does not belong" in res["error"]


def test_delete_execution_requires_confirm():
    ctx = make_context(YAML, responses={
        "GetTestsInTestExecution": exec_members(),
        "DeleteTestExecution": {"deleteTestExecution": "ok"}})
    res, err = call(ctx, "delete_test_execution", projectKey="P", testExecutionKey="P-50")
    assert not err and res["wouldDelete"]["testCount"] == 2 and "DeleteTestExecution" not in ops(ctx)
    res, err = call(ctx, "delete_test_execution", projectKey="P", testExecutionKey="P-50",
                    confirm=True)
    assert res == {"key": "P-50", "deleted": True}
    assert ctx.xray.calls_of("DeleteTestExecution") == [{"issueId": "500"}]


# ---- search / get

def test_search_executions_scoped_to_project():
    ctx = make_context(YAML, responses={"GetTestExecutions": {"getTestExecutions": {"results": [
        {"issueId": "1", "jira": {"key": "P-50", "summary": "Run"}},
        {"issueId": "2", "jira": {"key": "Q-50", "summary": "Other"}}]}}})
    res, err = call(ctx, "search_test_executions", projectKey="P", query="Run")
    assert res == {"testExecutions": [{"key": "P-50", "summary": "Run"}]}
    assert ctx.xray.calls_of("GetTestExecutions")[0]["jql"] == 'project = P AND summary ~ "Run"'
    res, err = call(ctx, "search_test_executions", projectKey="P")
    assert err and "query or jql" in res["error"]


def test_get_execution_paginated_compact_and_detail():
    rows = [{"status": {"name": "FAILED"}, "assigneeId": "a", "executedById": "b",
             "test": {"jira": {"key": "P-1", "summary": "One"}, "testType": {"name": "Manual"}},
             "steps": [{"status": {"name": "PASSED"}, "comment": None, "action": "x"},
                       {"status": {"name": "FAILED"}, "comment": "bad", "action": "y"}],
             "defects": ["P-9"]}]
    ctx = make_context(YAML, responses={
        "GetTestsInTestExecution": exec_members(),
        "GetTestExecutionRuns": {"getTestRuns": {"total": 5, "results": rows}}})
    res, err = call(ctx, "get_test_execution", projectKey="P", testExecutionKey="P-50",
                    limit=1, start=2)
    assert not err and res["total"] == 5 and res["start"] == 2 and res["key"] == "P-50"
    assert res["testRuns"] == [{"testKey": "P-1", "summary": "One", "status": "FAILED"}]
    assert ctx.xray.calls_of("GetTestExecutionRuns")[0] == {
        "testExecIssueIds": ["500"], "limit": 1, "start": 2}
    res, _ = call(ctx, "get_test_execution", projectKey="P", testExecutionKey="P-50", detail=True)
    r = res["testRuns"][0]
    assert r["testType"] == "Manual" and r["defects"] == ["P-9"] and r["executedById"] == "b"
    assert r["failedSteps"] == [{"index": 1, "action": "y", "status": "FAILED", "comment": "bad"}]


# ---- update_test_run

def test_update_run_maps_settings_test_run_fields():
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", fields={"version": "1.4", "build": "42"},
                    rawFields={"Other": "x"})
    assert not err, res
    upd = ctx.xray.calls_of("UpdateTestRun")[0]
    assert upd["id"] == "run-1"
    assert upd["customFields"] == [{"id": "cf-v", "value": "1.4"}, {"id": "cf_build", "value": "42"},
                                   {"id": "cf-o", "value": "x"}]
    assert "assigneeId" not in upd and res["customFieldsUpdated"] == ["version", "build", "Other"]
    assert res["status"] is None and "UpdateTestRunStatus" not in ops(ctx)


def test_update_run_unknown_run_field_name_refused_before_mutation():
    ctx = run_ctx(runs=run(custom=()))
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", fields={"version": "1.4"})
    assert err and "Version" in res["error"]
    assert not any(o.startswith(("Update", "Add")) for o in ops(ctx))


def run_with(*statuses):
    r = run(steps=len(statuses))
    for st, val in zip(r["getTestRuns"]["results"][0]["steps"], statuses):
        st["status"] = {"name": val}
    return r


def update_steps(runs, steps, **extra):
    ctx = run_ctx(runs=runs)
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", steps=steps, **extra)
    assert not err, res
    return ctx, res


def test_update_run_comment_only_keeps_run_status():
    ctx, res = update_steps(run_with("PASSED", "PASSED"), [{"index": 0, "comment": "note"}])
    assert res["status"] is None and "UpdateTestRunStatus" not in ops(ctx)
    assert ctx.xray.calls_of("UpdateTestRunStep")[0]["updateData"] == {"comment": "note"}


def test_update_run_partial_pass_with_other_steps_todo_is_executing():
    ctx, res = update_steps(run_with("TODO", "TODO", "TODO"), [{"index": 0, "status": "PASS"}])
    assert res["status"] == "EXECUTING"
    assert ctx.xray.calls_of("UpdateTestRunStatus") == [{"id": "run-1", "status": "EXECUTING"}]


def test_update_run_all_steps_pass_is_passed():
    ctx, res = update_steps(run_with("PASSED", "TODO", "TODO"),
                            [{"index": 1, "status": "pass"}, {"index": 2, "status": "PASSED"}])
    assert res["status"] == "PASSED"
    assert ctx.xray.calls_of("UpdateTestRunStatus") == [{"id": "run-1", "status": "PASSED"}]


def test_update_run_one_failed_step_is_failed():
    # a step that already failed keeps the run FAILED even when this call passes others
    _, res = update_steps(run_with("FAILED", "TODO"), [{"index": 1, "status": "PASS"}])
    assert res["status"] == "FAILED"
    ctx, res = update_steps(run_with("PASSED", "PASSED"), [{"index": 0, "status": "FAIL"}])
    assert res["status"] == "FAILED"
    assert ctx.xray.calls_of("UpdateTestRunStatus") == [{"id": "run-1", "status": "FAILED"}]


def test_update_run_explicit_status_wins():
    _, res = update_steps(run(), [{"index": 0, "status": "PASS"}], status="BLOCKED")
    assert res["status"] == "BLOCKED"


def test_update_run_applies_other_updates_before_status(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"img")
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", status="FAILED", assigneeId=None, executedById="u1",
                    fields={"version": "2"}, steps=[{"index": 1, "status": "FAIL",
                                                     "actualResult": "boom"}],
                    attachments=[str(f)], defectKeys=["P-9"])
    assert not err, res
    mutations = [o for o in ops(ctx) if o.startswith(("Update", "Add"))]
    assert mutations[-1] == "UpdateTestRunStatus"
    assert mutations[:-1] == ["UpdateTestRun", "UpdateTestRunStep", "AddDefectsToTestRun",
                              "AddEvidenceToTestRun"]
    upd = ctx.xray.calls_of("UpdateTestRun")[0]
    assert upd["assigneeId"] is None and upd["executedById"] == "u1"
    assert ctx.xray.calls_of("UpdateTestRunStep")[0] == {
        "testRunId": "run-1", "stepId": "s1",
        "updateData": {"status": "FAILED", "actualResult": "boom"}}
    ev = ctx.xray.calls_of("AddEvidenceToTestRun")[0]["evidence"][0]
    assert ev == {"filename": "shot.png", "mimeType": "image/png",
                  "data": base64.b64encode(b"img").decode("ascii")}
    assert res["defectsLinked"] == ["P-9"] and res["attachmentsUploaded"] == 1
    assert res["assigneeId"] is None and res["executedById"] == "u1"


def test_update_run_unknown_test_in_execution_is_error():
    ctx = run_ctx(runs={"getTestRuns": {"results": []}})
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", status="PASSED")
    assert err and "P-1" in res["error"] and "P-50" in res["error"]
    assert not any(o.startswith(("Update", "Add")) for o in ops(ctx))


def test_evidence_file_missing_is_error_before_any_call(tmp_path):
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", status="PASSED", attachments=[str(tmp_path / "nope.png")])
    assert err and "nope.png" in res["error"] and ctx.xray.calls == []


def test_evidence_relative_path_resolves_against_project_dir(tmp_path):
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "a.png").write_bytes(b"img")
    ctx = run_ctx()
    ctx.env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", attachments=["shots/a.png"])
    assert not err, res
    assert ctx.xray.calls_of("AddEvidenceToTestRun")[0]["evidence"][0]["filename"] == "a.png"
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", attachments=["shots"])
    assert err and "is a directory" in res["error"]


def test_update_run_step_index_out_of_range_before_mutation():
    ctx = run_ctx(runs=run(steps=2))
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", steps=[{"index": 0, "status": "PASS"},
                                          {"index": 2, "status": "PASS"}])
    assert err and "out of range" in res["error"]
    assert not any(o.startswith(("Update", "Add")) for o in ops(ctx))


def test_update_run_checks_keys_and_needs_something():
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="Q-50",
                    testKey="P-1", status="PASSED")
    assert err and "does not belong" in res["error"] and ctx.xray.calls == []
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50", testKey="P-1")
    assert err and "Nothing to update" in res["error"]
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", status="")
    assert err


def test_update_run_defects_may_be_cross_project_but_must_be_well_formed():
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", defectKeys=["Q-9", "P-9"])
    assert not err, res
    assert ctx.xray.calls_of("AddDefectsToTestRun") == [{"id": "run-1", "issues": ["Q-9", "P-9"]}]
    for bad in ("bad key", "Q9", "Q-9) OR (project=X"):
        ctx = run_ctx()
        res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                        testKey="P-1", defectKeys=[bad])
        assert err and res["error"] == "Invalid issue key" and res["key"] == bad
        assert ctx.xray.calls == []


def test_update_run_normalises_pass_fail_statuses():
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", steps=[{"index": 0, "status": "pass"},
                                          {"index": 1, "status": "Fail"},
                                          {"index": 2, "status": "todo"}])
    assert not err, res
    assert [c["updateData"]["status"] for c in ctx.xray.calls_of("UpdateTestRunStep")] == [
        "PASSED", "FAILED", "TODO"]
    ctx = run_ctx()
    res, _ = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                  testKey="P-1", status="pass")
    assert res["status"] == "PASSED"
    assert ctx.xray.calls_of("UpdateTestRunStatus") == [{"id": "run-1", "status": "PASSED"}]


def test_execution_key_injection_refused_before_call():
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P",
                    testExecutionKey="P-1) OR (project=Q", testKey="P-1", status="PASSED")
    assert err and res["error"] == "Invalid issue key" and ctx.xray.calls == []


def test_update_run_partial_failure_lists_applied():
    def fail(_):
        raise RuntimeError("defect boom")
    ctx = run_ctx(AddDefectsToTestRun=fail)
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", status="FAILED", steps=[{"index": 0, "status": "FAIL"}],
                    defectKeys=["P-9"])
    assert err and res["applied"] == ["step 0"] and "defect boom" in res["cause"]
    assert "UpdateTestRunStatus" not in ops(ctx)


def test_update_run_partial_failure_from_missing_permission_is_flagged():
    from xray_cloud.errors import ToolError

    def denied(_):
        raise ToolError("You do not have permission to edit issues in this project")
    ctx = run_ctx(AddDefectsToTestRun=denied)
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", steps=[{"index": 0, "comment": "c"}], defectKeys=["P-9"])
    assert err and res["applied"] == ["step 0"] and res["reason"] == "permission_denied"


def test_update_run_required_run_field_enforced_only_when_setting_status():
    yaml = YAML.replace("version: {name: Version}", "version: {name: Version, required: true}")
    ctx = run_ctx()
    ctx = make_context(yaml, responses=dict(ctx.xray.responses))
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", status="PASSED")
    assert err and res["error"] == "Missing required fields" and ctx.xray.calls == []
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", executedById="u1")
    assert not err, res


@pytest.mark.parametrize("given,expect_in_mutation", [
    ({"assigneeId": ""}, True), ({"assigneeId": None}, True), ({}, False)])
def test_update_run_unassign_with_empty_string_or_null_omitted_unchanged(given, expect_in_mutation):
    ctx = run_ctx()
    res, err = call(ctx, "update_test_run", projectKey="P", testExecutionKey="P-50",
                    testKey="P-1", fields={"version": "1.4"}, **given)
    assert not err, res
    upd = ctx.xray.calls_of("UpdateTestRun")[0]
    assert ("assigneeId" in upd) == expect_in_mutation
    if expect_in_mutation:
        assert upd["assigneeId"] is None
