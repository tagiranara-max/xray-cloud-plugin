from __future__ import annotations

import json

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

Y = "projects:\n  P: {}\n"


def call(ctx, **args):
    res = REGISTRY.call(ctx, "set_lifecycle_label", args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def ctx_with(labels, y=Y):
    ctx = make_context(y, responses={"SearchTests": {"getTests": {"results": [
        {"issueId": "1", "jira": {"key": "P-1", "summary": "s", "labels": labels}}]}}})

    class J:
        updates = []

        def edit_issue(self, k, f):
            self.updates.append((k, f))
    ctx.jira = J()
    return ctx


def test_set_lifecycle_label_replaces_previous_lifecycle_label():
    ctx = ctx_with(["keep", "ai-draft", "obsolete"])
    res, err = call(ctx, projectKey="P", testKey="P-1", label="automated")
    assert not err
    assert ctx.jira.updates == [("P-1", {"labels": ["keep", "automated"]})]


def test_set_lifecycle_label_rejects_label_not_in_settings():
    ctx = ctx_with([], "lifecycleLabels: [a, b]\nprojects:\n  P: {}\n")
    res, err = call(ctx, projectKey="P", testKey="P-1", label="ai-draft")
    assert err and res["allowed"] == ["a", "b"] and ctx.xray.calls == []


def test_set_lifecycle_label_without_token_returns_guidance():
    ctx = ctx_with([])
    ctx.jira = None
    res, err = call(ctx, projectKey="P", testKey="P-1", label="ai-draft")
    assert err and "no Jira token or Atlassian MCP" in res["error"]
