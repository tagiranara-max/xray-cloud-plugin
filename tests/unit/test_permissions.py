"""Permission-denied errors from Xray/Jira become clear, stable ToolErrors."""
from __future__ import annotations

import json

import pytest

from conftest import FakeTransport, json_response, make_context
from xray_cloud.config import JiraCredentials
from xray_cloud.context import is_permission_error, permission_denied, permission_guard
from xray_cloud.errors import ToolError
from xray_cloud.jira_client import JiraClient
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = "projects:\n  P: {}\n"
XRAY_DELETE_MSG = ("Delete issue 10005 failed! - You do not have permission to delete issues "
                   "in this project.")


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def gql_error(msg):
    def raise_(_):
        raise ToolError(msg, graphqlErrors=[{"message": msg}])
    return raise_


def test_detection_text_case_insensitive_and_403():
    assert is_permission_error(ToolError(XRAY_DELETE_MSG))
    assert is_permission_error(ToolError("YOU DO NOT HAVE PERMISSION TO EDIT"))
    assert is_permission_error(ToolError("x", graphqlErrors=[{"message": "User does not have "
                                                                         "permission to create"}]))
    assert is_permission_error(ToolError("Xray request failed with HTTP 403: nope", status=403))
    assert not is_permission_error(ToolError("Test not found: P-1"))
    assert not is_permission_error(ToolError("boom", status=500))
    assert not is_permission_error(ValueError("do not have permission"))


def test_guard_maps_permission_and_passes_others_unchanged():
    with pytest.raises(ToolError) as ei:
        with permission_guard("delete", "P", "P-1"):
            raise ToolError(XRAY_DELETE_MSG)
    e = ei.value
    assert e.message.startswith("You don't have permission to delete issues in project P. ")
    assert "'Delete Issues'" in e.message and "Administrator role" in e.message
    assert e.data == {"reason": "permission_denied", "permission": "DELETE_ISSUES",
                      "projectKey": "P", "key": "P-1", "xrayMessage": XRAY_DELETE_MSG}
    other = ToolError("Test not found", testKey="P-1")
    with pytest.raises(ToolError) as ei:
        with permission_guard("delete", "P", "P-1"):
            raise other
    assert ei.value is other


def test_non_delete_action_keeps_original_message():
    e = permission_denied(ToolError("You do not have permission to create issues"),
                          action="run create_test", project_key="P")
    assert e.message.startswith("You don't have permission to run create_test in project P.")
    assert e.data == {"reason": "permission_denied", "action": "run create_test",
                      "projectKey": "P", "xrayMessage": "You do not have permission to create issues"}


def _set(kind, key="P-10"):
    return {"get%ss" % kind: {"results": [{"issueId": "900", "jira": {"key": key, "summary": "S"},
                                           "tests": {"total": 0, "results": []}}]}}


@pytest.mark.parametrize("tool,args,responses", [
    ("delete_test", {"testKey": "P-1"},
     {"SearchTests": {"getTests": {"results": [{"issueId": "100", "jira": {"key": "P-1",
                                                                            "summary": "s"}}]}},
      "DeleteTest": gql_error(XRAY_DELETE_MSG)}),
    ("delete_test_set", {"testSetKey": "P-10"},
     {"GetTestsInTestSet": _set("TestSet"), "DeleteTestSet": gql_error(XRAY_DELETE_MSG)}),
    ("delete_test_plan", {"testPlanKey": "P-10"},
     {"GetTestsInTestPlan": _set("TestPlan"), "DeleteTestPlan": gql_error(XRAY_DELETE_MSG)}),
    ("delete_test_execution", {"testExecutionKey": "P-10"},
     {"GetTestsInTestExecution": _set("TestExecution"),
      "DeleteTestExecution": gql_error(XRAY_DELETE_MSG)}),
])
def test_delete_tools_report_permission_denied(tool, args, responses):
    ctx = make_context(YAML, responses=responses)
    res, err = call(ctx, tool, projectKey="P", confirm=True, **args)
    assert err
    assert res["error"].startswith("You don't have permission to delete issues in project P.")
    assert res["reason"] == "permission_denied" and res["permission"] == "DELETE_ISSUES"
    assert res["projectKey"] == "P" and res["key"] == list(args.values())[0]
    assert res["xrayMessage"] == XRAY_DELETE_MSG


def test_delete_folder_permission_denied():
    ctx = make_context(YAML, responses={
        "GetFolder": {"getFolder": {"name": "a", "path": "/a", "testsCount": 0, "folders": []}},
        "DeleteFolder": gql_error("You do not have permission to manage the test repository")})
    ctx.project_ids["P"] = "1"
    res, err = call(ctx, "delete_folder", projectKey="P", path="/a", confirm=True,
                    numericProjectId="1")
    assert err and res["reason"] == "permission_denied" and res["action"] == "delete folder /a"
    assert "manage the test repository" in res["xrayMessage"]


def test_other_write_errors_mapped_generically_and_403():
    ctx = make_context(YAML, responses={
        "SearchTests": {"getTests": {"results": [{"issueId": "100", "jira": {"key": "P-1"}}]}},
        "UpdateTestType": gql_error("You do not have permission to edit issues in this project.")})
    res, err = call(ctx, "update_test_type", projectKey="P", testKey="P-1", testType="Generic")
    assert err and res["reason"] == "permission_denied" and res["action"] == "run update_test_type"
    assert res["error"].startswith("You don't have permission to run update_test_type in project P.")
    assert res["xrayMessage"] == "You do not have permission to edit issues in this project."

    def http403(_):
        raise ToolError("Xray request failed with HTTP 403: forbidden", status=403)
    ctx = make_context(YAML, responses={"SearchTests": {"getTests": {"results": [
        {"issueId": "100", "jira": {"key": "P-1"}}]}}, "UpdateTestType": http403})
    res, err = call(ctx, "update_test_type", projectKey="P", testKey="P-1", testType="Generic")
    assert err and res["reason"] == "permission_denied"


def test_non_permission_errors_and_reads_unchanged():
    ctx = make_context(YAML, responses={
        "SearchTests": {"getTests": {"results": [{"issueId": "100", "jira": {"key": "P-1"}}]}},
        "DeleteTest": gql_error("Something else broke")})
    res, err = call(ctx, "delete_test", projectKey="P", testKey="P-1", confirm=True)
    assert err and res["error"] == "Something else broke" and "reason" not in res

    def http403(_):
        raise ToolError("Xray request failed with HTTP 403: forbidden", status=403)
    ctx = make_context(YAML, responses={"SearchTests": http403})
    res, err = call(ctx, "search_tests", projectKey="P", query="x")
    assert err and "reason" not in res  # read tools are not rewritten


class PermJira:
    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = []

    def has_permission(self, project_key, permission):
        self.calls.append((project_key, permission))
        if isinstance(self.allowed, Exception):
            raise self.allowed
        return self.allowed


def test_delete_preview_unchanged_without_token_and_can_delete_with_token():
    responses = {"SearchTests": {"getTests": {"results": [
        {"issueId": "100", "jira": {"key": "P-1", "summary": "s"}}]}}}
    ctx = make_context(YAML, responses=responses)
    res, _ = call(ctx, "delete_test", projectKey="P", testKey="P-1")
    assert res == {"wouldDelete": {"key": "P-1", "summary": "s"}, "hint": "pass confirm: true"}
    for allowed in (True, False):
        ctx = make_context(YAML, responses=responses)
        ctx.jira = PermJira(allowed)
        res, _ = call(ctx, "delete_test", projectKey="P", testKey="P-1")
        assert res["canDelete"] is allowed and ctx.jira.calls == [("P", "DELETE_ISSUES")]
    ctx = make_context(YAML, responses=responses)
    ctx.jira = PermJira(ToolError("Jira down"))
    res, err = call(ctx, "delete_test", projectKey="P", testKey="P-1")
    assert not err and "canDelete" not in res


def test_jira_has_permission():
    fake = FakeTransport().push(json_response({"permissions": {"DELETE_ISSUES": {
        "key": "DELETE_ISSUES", "havePermission": False}}}))
    jc = JiraClient(JiraCredentials("https://a.atlassian.net", "e@x.c", "tok"), fake)
    assert jc.has_permission("P", "DELETE_ISSUES") is False
    assert fake.calls[0].url == ("https://a.atlassian.net/rest/api/3/mypermissions"
                                 "?projectKey=P&permissions=DELETE_ISSUES")


def test_bare_403_on_delete_uses_generic_wording():
    with pytest.raises(ToolError) as ei:
        with permission_guard("delete", "P", "P-1"):
            raise ToolError("Xray request failed with HTTP 403: forbidden", status=403)
    e = ei.value
    assert "permission" not in e.data and "Delete Issues" not in e.message
    assert e.data["reason"] == "permission_denied" and e.data["action"] == "delete"
    assert e.data["xrayMessage"] == "Xray request failed with HTTP 403: forbidden"
    assert "grant you the required project permission (" in e.message  # no quotes


def test_not_found_or_no_view_permission_is_not_mapped():
    for msg in ("Issue does not exist or you do not have permission to see it.",
                "Test not found or you do not have permission to view it",
                "You do not have permission to browse this project"):
        assert not is_permission_error(ToolError(msg)), msg
        assert not is_permission_error(ToolError(msg, status=403)), msg
    with pytest.raises(ToolError) as ei:
        with permission_guard("delete", "P", "P-1"):
            raise ToolError("Issue does not exist or you do not have permission to see it.")
    assert "reason" not in ei.value.data


def test_partial_failure_marks_permission_causes():
    from xray_cloud.context import partial_failure
    e = partial_failure("x", ["P-1"], ToolError(XRAY_DELETE_MSG))
    assert e.data["reason"] == "permission_denied" and e.data["permission"] == "DELETE_ISSUES"
    assert e.data["created"] == ["P-1"] and e.data["cause"] == XRAY_DELETE_MSG
    e = partial_failure("x", [], ToolError("You do not have permission to edit issues"))
    assert e.data["reason"] == "permission_denied" and "permission" not in e.data
    mapped = permission_denied(ToolError(XRAY_DELETE_MSG), action="delete", project_key="P")
    e = partial_failure("x", [], mapped)
    assert e.data["reason"] == "permission_denied" and e.data["permission"] == "DELETE_ISSUES"
    e = partial_failure("x", [], ToolError("boom"))
    assert "reason" not in e.data
    e = partial_failure("x", [], ValueError("do not have permission"))
    assert "reason" not in e.data


def test_folder_create_partial_failure_reports_permission():
    def denied(_):
        raise ToolError("You do not have permission to manage folders")
    ctx = make_context(YAML, responses={
        "SearchTests": {"getTests": {"results": []}},
        "CreateTest": {"createTest": {"test": {"issueId": "1", "jira": {"key": "P-1"}},
                                      "warnings": []}},
        "GetProjectSettings": {"getProjectSettings": {"projectId": "77"}},
        "CreateFolder": denied})
    res, err = call(ctx, "create_test", projectKey="P", summary="s", aiLabel="", folderPath="/a",
                    allowDuplicates=True)
    assert err and res["created"] == ["P-1"] and res["reason"] == "permission_denied"
