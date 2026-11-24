"""Test environment tools. Xray Cloud has no API to create or delete an environment on its own
(verified against the live GraphQL schema): environments are listed per project
(getProjectSettings.testEnvironments), created implicitly when first added to a test execution,
and associated/disassociated per execution."""
from __future__ import annotations

from typing import Any, Dict, List

from ..context import ServerContext, check_issue_in_project, dedupe, require_project
from ..errors import ToolError
from ..registry import REGISTRY
from .tests import _schema

_GET = """
query GetProjectTestEnvironments($projectIdOrKey: String!) {
  getProjectSettings(projectIdOrKey: $projectIdOrKey) { testEnvironments }
}
"""
_ADD = """
mutation AddTestEnvironmentsToTestExecution($issueId: String!, $testEnvironments: [String]!) {
  addTestEnvironmentsToTestExecution(issueId: $issueId, testEnvironments: $testEnvironments) {
    associatedTestEnvironments
    createdTestEnvironments
    warning
  }
}
"""
_REMOVE = """
mutation RemoveTestEnvironmentsFromTestExecution($issueId: String!,
                                                 $testEnvironments: [String]!) {
  removeTestEnvironmentsFromTestExecution(issueId: $issueId, testEnvironments: $testEnvironments)
}
"""
_EXEC_ENVS = """
query GetTestExecutionEnvironments($jql: String!) {
  getTestExecutions(jql: $jql, limit: 1) {
    results { issueId testEnvironments jira(fields: ["key"]) }
  }
}
"""
_ENVS = {"type": "array", "minItems": 1, "items": {"type": "string"},
         "description": "Environment names, e.g. [\"staging\"]"}


def _list(ctx: ServerContext, project_key: str) -> List[str]:
    data = ctx.xray.graphql(_GET, {"projectIdOrKey": project_key})
    return list((data.get("getProjectSettings") or {}).get("testEnvironments") or [])


def _clean(names: List[str]) -> List[str]:
    out = [n.strip() for n in names]
    if any(not n for n in out):
        raise ToolError("Environment names must not be empty")
    return dedupe(out)


def _execution(ctx: ServerContext, pk: str, key: str) -> Dict[str, Any]:
    check_issue_in_project(key, pk)
    data = ctx.xray.graphql(_EXEC_ENVS, {"jql": "project = %s AND issue = %s" % (pk, key)})
    results = ((data.get("getTestExecutions") or {}).get("results")) or []
    if not results:
        raise ToolError("Test execution not found: %s" % key, key=key)
    return results[0]


@REGISTRY.tool(
    "get_test_environments",
    "List the test environments known in a project. Use before create_test_execution to "
    "check environment names.",
    _schema({}, []))
def get_test_environments(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=False)
    return {"environments": _list(ctx, pk)}


@REGISTRY.tool(
    "add_test_environments_to_test_execution",
    "Add test environments to a test execution. Xray has no separate 'create environment' "
    "call: an environment name that does not exist yet is created by this (reported in "
    "createdEnvironments).",
    _schema({"testExecutionKey": {"type": "string"}, "environments": _ENVS},
            ["testExecutionKey", "environments"]), write=True)
def add_test_environments_to_test_execution(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=True)
    names = _clean(args["environments"])
    ex = _execution(ctx, pk, args["testExecutionKey"])
    r = (ctx.xray.graphql(_ADD, {"issueId": ex["issueId"], "testEnvironments": names})
         .get("addTestEnvironmentsToTestExecution") or {})
    out: Dict[str, Any] = {"key": args["testExecutionKey"],
                           "associatedEnvironments": r.get("associatedTestEnvironments") or [],
                           "createdEnvironments": r.get("createdTestEnvironments") or []}
    if r.get("warning"):
        out["warnings"] = [r["warning"]]
    return out


@REGISTRY.tool(
    "remove_test_environments_from_test_execution",
    "Remove test environments from a test execution (the environments themselves cannot be "
    "deleted through the Xray API). Names not on the execution are reported as notPresent.",
    _schema({"testExecutionKey": {"type": "string"}, "environments": _ENVS},
            ["testExecutionKey", "environments"]), write=True)
def remove_test_environments_from_test_execution(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=True)
    names = _clean(args["environments"])
    ex = _execution(ctx, pk, args["testExecutionKey"])
    current = list(ex.get("testEnvironments") or [])
    present = [n for n in names if n in current]
    if present:
        ctx.xray.graphql(_REMOVE, {"issueId": ex["issueId"], "testEnvironments": present})
    return {"key": args["testExecutionKey"], "removed": present,
            "notPresent": [n for n in names if n not in current]}
