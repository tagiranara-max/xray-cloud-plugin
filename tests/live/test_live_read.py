"""Read-only live checks: auth in the configured region, search, folders, environments."""
from __future__ import annotations

import os

import pytest


def test_auth_in_configured_region(live):
    region = (os.environ.get("XRAY_REGION") or "global").lower()
    assert live.ctx.xray._creds.region == region
    data = live.gql("query { getStatuses { name } }")
    assert {"PASSED", "FAILED"} <= {s["name"] for s in data["getStatuses"]}


def test_search_tests(wlive):
    res = wlive.call("search_tests", jql="issuetype = Test", limit=5)
    assert isinstance(res["tests"], list)
    for t in res["tests"]:
        assert t["key"].startswith(wlive.project + "-") and "summary" in t
    if res["tests"]:
        one = wlive.call("get_test", testKey=res["tests"][0]["key"], detail=True)
        assert one["key"] == res["tests"][0]["key"] and "testType" in one


def test_list_folders_root(wlive):
    res = wlive.call("list_folders")
    assert res["tree"]["path"] == "/" and isinstance(res["tree"]["children"], list)


def test_get_test_environments(wlive):
    res = wlive.call("get_test_environments")
    assert isinstance(res["environments"], list)


def test_get_create_fields_and_versions(jlive, results_log):
    res = jlive.call("get_create_fields", issueType="Test")
    assert res["source"] == "jira"
    ids = {f["id"] for f in res["fields"]}
    assert {"summary", "project", "issuetype"} <= ids, ids
    summary = next(f for f in res["fields"] if f["id"] == "summary")
    assert summary["required"] is True and summary["schemaType"] == "string"
    versions = jlive.call("list_project_versions")
    assert isinstance(versions["versions"], list)
    results_log["createmeta"] = {"fieldCount": len(ids), "versions": len(versions["versions"])}


def test_unknown_project_rejected(live):
    res = live.call_error("search_tests", projectKey="NOPE", query="x")
    assert res["error"] == "Unknown Xray project key"
