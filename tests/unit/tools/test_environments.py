from __future__ import annotations

import json

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = "projects:\n  P: {}\n"


def call(ctx, _tool, **args):
    res = REGISTRY.call(ctx, _tool, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def ctx_with(envs=("staging", "prod"), exec_envs=("staging",)):
    return make_context(YAML, responses={
        "GetProjectTestEnvironments": {"getProjectSettings": {"testEnvironments": list(envs)}},
        "GetTestExecutionEnvironments": {"getTestExecutions": {"results": [
            {"issueId": "50", "testEnvironments": list(exec_envs), "jira": {"key": "P-50"}}]}},
        "AddTestEnvironmentsToTestExecution": {"addTestEnvironmentsToTestExecution": {
            "associatedTestEnvironments": ["qa"], "createdTestEnvironments": ["qa"],
            "warning": None}},
        "RemoveTestEnvironmentsFromTestExecution": {
            "removeTestEnvironmentsFromTestExecution": "ok"}})


def test_get_environments_reads_project_settings_and_validates_project():
    ctx = ctx_with()
    res, err = call(ctx, "get_test_environments", projectKey="P")
    assert res == {"environments": ["staging", "prod"]}
    assert ctx.xray.calls_of("GetProjectTestEnvironments") == [{"projectIdOrKey": "P"}]
    res, err = call(ctx, "get_test_environments", projectKey="Z")
    assert err and res["error"] == "Unknown Xray project key"


def test_get_environments_none_configured():
    ctx = make_context(YAML, responses={
        "GetProjectTestEnvironments": {"getProjectSettings": {"testEnvironments": None}}})
    assert call(ctx, "get_test_environments", projectKey="P")[0] == {"environments": []}


def test_add_environments_to_execution_reports_created():
    ctx = ctx_with()
    res, err = call(ctx, "add_test_environments_to_test_execution", projectKey="P",
                    testExecutionKey="P-50", environments=[" qa ", "qa"])
    assert not err, res
    assert res == {"key": "P-50", "associatedEnvironments": ["qa"], "createdEnvironments": ["qa"]}
    assert ctx.xray.calls_of("AddTestEnvironmentsToTestExecution") == [
        {"issueId": "50", "testEnvironments": ["qa"]}]


def test_remove_environments_only_present_ones():
    ctx = ctx_with()
    res, err = call(ctx, "remove_test_environments_from_test_execution", projectKey="P",
                    testExecutionKey="P-50", environments=["staging", "prod"])
    assert res == {"key": "P-50", "removed": ["staging"], "notPresent": ["prod"]}
    assert ctx.xray.calls_of("RemoveTestEnvironmentsFromTestExecution") == [
        {"issueId": "50", "testEnvironments": ["staging"]}]


def test_environment_tools_validate_keys_and_names():
    ctx = ctx_with()
    res, err = call(ctx, "add_test_environments_to_test_execution", projectKey="P",
                    testExecutionKey="Q-50", environments=["qa"])
    assert err and "does not belong" in res["error"] and ctx.xray.calls == []
    res, err = call(ctx, "add_test_environments_to_test_execution", projectKey="P",
                    testExecutionKey="P-50", environments=["  "])
    assert err and ctx.xray.calls == []


def test_environment_writes_blocked_read_only():
    ctx = make_context(YAML, read_only=True)
    res, err = call(ctx, "add_test_environments_to_test_execution", projectKey="P",
                    testExecutionKey="P-50", environments=["qa"])
    assert err and "read-only" in res["error"]
