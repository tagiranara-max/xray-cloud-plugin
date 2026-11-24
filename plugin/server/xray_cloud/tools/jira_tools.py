from __future__ import annotations

from ..context import require_project
from ..errors import ToolError
from ..registry import REGISTRY
from .tests import _schema, require_jira

# Xray issue type name -> settings file item type
_SETTINGS_TYPES = {"test": "test", "test execution": "testExecution", "test plan": "testPlan",
                   "test set": "testSet", "precondition": "precondition"}
_XRAY_TYPE_NAMES = ["Test", "Test Execution", "Test Plan", "Test Set", "Precondition"]


@REGISTRY.tool(
    "get_create_fields",
    "List the fields of a Jira issue type in a project (required/optional, type, allowed "
    "values) from Jira create-meta (source: jira, needs the optional Jira API token). Without "
    "the token it returns the fields declared in the settings file (source: settings). "
    "issueType is e.g. Test, Test Execution, Test Plan, Test Set, Precondition.",
    _schema({"issueType": {"type": "string"}}, ["issueType"]),
    write=False)
def get_create_fields(ctx, args):
    pk, issue_type = args["projectKey"], args["issueType"]
    project = require_project(ctx, pk, write=False)
    jira = ctx.jira
    if jira is not None:
        return {"source": "jira", "projectKey": pk, "issueType": issue_type,
                "fields": jira.create_meta(pk, issue_type)}
    item_type = _SETTINGS_TYPES.get(issue_type.strip().lower())
    if item_type is None:
        raise ToolError("Without the Jira API token only these issue types are known from the "
                        "settings file: %s" % ", ".join(_XRAY_TYPE_NAMES),
                        knownIssueTypes=_XRAY_TYPE_NAMES)
    fields = [{"name": s.name, "id": s.id or s.xray_name, "required": s.required,
               "schemaType": s.type, "default": s.default}
              for s in (project.fields.get(item_type) or {}).values()]
    return {"source": "settings", "projectKey": pk, "issueType": issue_type, "fields": fields}


@REGISTRY.tool(
    "list_project_versions",
    "List the versions of a Jira project (for validating fixVersion/affectsVersion names). "
    "Needs the optional Jira API token.",
    _schema({}, []),
    write=False)
def list_project_versions(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=False)
    jira = require_jira(ctx)
    return {"projectKey": pk, "versions": jira.versions(pk)}
