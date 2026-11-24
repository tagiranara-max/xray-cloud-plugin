from __future__ import annotations

import json

from conftest import FakeTransport, json_response, make_context
from xray_cloud.config import JiraCredentials
from xray_cloud.http import Response
from xray_cloud.jira_client import JiraClient
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

Y = """
projects:
  P:
    fields:
      test:
        Severity: {id: customfield_1, type: option, required: true}
"""
from xray_cloud.tools.tests import JIRA_TOKEN_MESSAGE as MSG  # noqa: E402
TOKEN = "tok-SECRET"


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def with_jira(ctx, fake):
    ctx.jira = JiraClient(JiraCredentials("https://a.atlassian.net", "e@x.c", TOKEN), fake)
    return ctx


def test_tools_registered_read_only_strict_schema():
    for name in ("get_create_fields", "list_project_versions"):
        t = REGISTRY.get(name)
        assert t.write is False
        assert t.params["additionalProperties"] is False and "projectKey" in t.params["required"]


def test_get_create_fields_without_token_uses_settings():
    ctx = make_context(Y)
    res, err = call(ctx, "get_create_fields", projectKey="P", issueType="Test")
    assert not err and res["source"] == "settings"
    assert res["fields"] == [{"name": "Severity", "id": "customfield_1", "required": True,
                              "schemaType": "option", "default": None}]


def test_get_create_fields_without_token_unknown_type():
    res, err = call(make_context(Y), "get_create_fields", projectKey="P", issueType="Epic")
    assert err and "Precondition" in res["knownIssueTypes"]


def test_get_create_fields_with_token():
    fake = FakeTransport().push(
        json_response({"issueTypes": [{"id": "10", "name": "Test"}], "isLast": True}),
        json_response({"fields": [{"fieldId": "summary", "name": "Summary", "required": True,
                                   "schema": {"type": "string"}}], "isLast": True}))
    ctx = with_jira(make_context(Y), fake)
    res, err = call(ctx, "get_create_fields", projectKey="P", issueType="Test")
    assert not err and res["source"] == "jira" and res["fields"][0]["id"] == "summary"


def test_get_create_fields_403_message():
    fake = FakeTransport().push(Response(403, {}, b"{}"))
    ctx = with_jira(make_context(Y), fake)
    res, err = call(ctx, "get_create_fields", projectKey="P", issueType="Test")
    assert err and "lacks permission" in res["error"] and TOKEN not in json.dumps(res)


def test_list_project_versions_without_token_message():
    res, err = call(make_context(Y), "list_project_versions", projectKey="P")
    assert err and res["error"] == MSG


def test_list_project_versions_with_token():
    fake = FakeTransport().push(json_response([{"id": "1", "name": "1.0"}]))
    ctx = with_jira(make_context(Y), fake)
    res, err = call(ctx, "list_project_versions", projectKey="P")
    assert not err and res["versions"][0]["name"] == "1.0"


def test_edit_test_with_real_client_sends_adf():
    fake = FakeTransport().push(Response(204, {}, b""))
    ctx = with_jira(make_context(Y), fake)
    res, err = call(ctx, "edit_test", projectKey="P", testKey="P-1", description="l1\nl2",
                    priority="High")
    assert not err
    body = json.loads(fake.calls[0].body)["fields"]
    assert body["priority"] == {"name": "High"}
    assert body["description"]["type"] == "doc" and len(body["description"]["content"]) == 2
