from __future__ import annotations

import json

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401
from xray_cloud.notdone import NO_JIRA_FIX, NO_JIRA_REASON


def desc(name):
    return next(t["description"] for t in REGISTRY.list() if t["name"] == name)


def test_create_tools_explain_needs_answer():
    for name in ("create_test", "create_test_set", "create_test_plan"):
        d = desc(name)
        assert "needs_answer" in d and "ask the user once" in d and "remembered" in d, name


def test_feature_import_guidance():
    d = desc("create_tests_from_feature")
    # the preview has no status key: questions come as a non-empty `questions` list
    assert "non-empty `questions`" in d and "needs_answer" not in d
    assert "notDone" in d and "Atlassian MCP" in d


def test_analyze_feature_tags_marks_examples():
    assert "example" in desc("analyze_feature_tags")


def test_get_status_mentions_how_to_change_choices():
    d = desc("get_status")
    assert "set_preferences" in d and "settings file" in d and "source" in d


def test_no_token_errors_use_shared_wording():
    ctx = make_context("projects:\n  P: {}\n")
    ctx.jira = None
    for name, args in (("set_lifecycle_label", {"testKey": "P-1", "label": "ai-draft"}),
                       ("edit_test", {"testKey": "P-1", "summary": "x"})):
        res = REGISTRY.call(ctx, name, dict(args, projectKey="P"))
        body = json.loads(res["content"][0]["text"])
        assert res.get("isError")
        assert NO_JIRA_REASON in body["error"] and NO_JIRA_FIX in body["error"], name
        assert body["reason"] == NO_JIRA_REASON and body["fix"] == NO_JIRA_FIX
