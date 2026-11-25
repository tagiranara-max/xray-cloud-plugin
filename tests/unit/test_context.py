from __future__ import annotations

import pytest

from conftest import make_context
from xray_cloud.config import SettingsStore
from xray_cloud.context import (NO_CREDENTIALS_MESSAGE, ServerContext, check_issue_in_project,
                                compact, confirm_or_preview, partial_failure,
                                require_project, require_write, resolve_project_id)
from xray_cloud.errors import ToolError
from xray_cloud.registry import Registry
from xray_cloud.stepcache import StepCache

YAML = "projects:\n  PROJ: {}\n  ALPHA: {}\n"


def test_context_builds_without_credentials_and_xray_raises_lazily():
    env = {}
    ctx = ServerContext(env, SettingsStore(env), None)
    assert ctx.jira is None and ctx.read_only is False
    assert isinstance(ctx.step_cache, StepCache) and len(ctx.folder_cache._entries) == 0
    with pytest.raises(ToolError, match="Xray credentials are not configured") as ei:
        ctx.xray
    assert "XRAY_CLIENT_ID" in str(ei.value) and "/plugin" in str(ei.value)
    assert NO_CREDENTIALS_MESSAGE == ei.value.message


def test_xray_built_lazily_from_credentials():
    from xray_cloud.config import Credentials
    from xray_cloud.xray_client import XrayClient
    env = {}
    ctx = ServerContext(env, SettingsStore(env), Credentials("i", "s", "global", "https://x"))
    assert isinstance(ctx.xray, XrayClient) and ctx.xray is ctx.xray


def test_require_project_known(tmp_path):
    ctx = make_context(YAML, tmp_path=tmp_path)
    assert require_project(ctx, "PROJ", write=True).key == "PROJ"


def test_require_project_unknown_lists_known_for_reads(tmp_path):
    ctx = make_context(YAML, tmp_path=tmp_path)
    with pytest.raises(ToolError, match="Unknown Xray project key") as ei:
        require_project(ctx, "NOPE", write=False)
    assert ei.value.data["known"] == ["ALPHA", "PROJ"]


def test_require_project_unlisted_write_goes_through_approval(tmp_path):
    # Without credentials no approval can be stored: the no-credentials message, not
    # "Unknown Xray project key" (approval itself is covered in test_approval.py).
    ctx = make_context(YAML, tmp_path=tmp_path)
    with pytest.raises(ToolError) as ei:
        require_project(ctx, "NOPE", write=True)
    assert ei.value.message == NO_CREDENTIALS_MESSAGE


def test_require_project_no_file_write_goes_through_approval(tmp_path):
    ctx = make_context(tmp_path=tmp_path)
    with pytest.raises(ToolError) as ei:
        require_project(ctx, "P", write=True)
    assert ei.value.message == NO_CREDENTIALS_MESSAGE


def test_require_project_no_file_read_returns_empty(tmp_path):
    ps = require_project(make_context(tmp_path=tmp_path), "P", write=False)
    assert ps.key == "P" and ps.fields == {}


def test_check_issue_in_project():
    check_issue_in_project("P-1", "P")
    for bad in ("Q-1", "P-1-2x", "P1", "PP-1"):
        with pytest.raises(ToolError):
            check_issue_in_project(bad, "P")


def test_check_issue_rejects_malformed_keys_before_project_check():
    for bad in ("P-1) OR (project=Q", "p-1", "P-0", "P-01", " P-1", "P-1 ", "P-1\n", "1P-1"):
        with pytest.raises(ToolError) as e:
            check_issue_in_project(bad, "P")
        assert e.value.message == "Invalid issue key" and e.value.data["key"] == bad
    check_issue_in_project("PROJ_2-10", "PROJ_2")


def test_check_issue_project_prefix_must_match_exactly():
    with pytest.raises(ToolError, match="does not belong"):
        check_issue_in_project("PROJ2-1", "PROJ")
    check_issue_in_project("PROJ2-1", "PROJ2")


def test_issue_key_project_mismatch_rejected():
    with pytest.raises(ToolError, match="Q-1"):
        check_issue_in_project("Q-1", "P")


def test_require_write(tmp_path):
    require_write(make_context(tmp_path=tmp_path))
    with pytest.raises(ToolError, match="read-only"):
        require_write(make_context(read_only=True, tmp_path=tmp_path))


def test_write_tool_blocked_in_read_only(tmp_path):
    reg = Registry()
    ran = []
    reg.tool("w", "d", {"type": "object", "properties": {}}, write=True)(lambda c, a: ran.append(1))
    reg.tool("r", "d", {"type": "object", "properties": {}})(lambda c, a: "ok")
    ctx = make_context(read_only=True, tmp_path=tmp_path)
    res = reg.call(ctx, "w", {})
    assert res["isError"] and "read-only" in res["content"][0]["text"] and not ran
    assert "isError" not in reg.call(ctx, "r", {})
    assert "isError" not in reg.call(make_context(tmp_path=tmp_path), "w", {})


def test_confirm_or_preview():
    assert confirm_or_preview(True, {"a": 1}) is None
    assert confirm_or_preview(False, {"a": 1}) == {"wouldDelete": {"a": 1},
                                                  "hint": "pass confirm: true"}


def test_partial_failure_lists_created_keys():
    err = partial_failure("adding steps failed", ["P-1"], RuntimeError("boom"))
    assert err.data == {"created": ["P-1"], "cause": "boom"}
    assert err.to_dict() == {"error": "adding steps failed", "created": ["P-1"], "cause": "boom"}
    assert isinstance(err, ToolError)


def test_resolve_project_id_from_project_settings_cached(tmp_path):
    ctx = make_context(responses={
        "GetProjectSettings": {"getProjectSettings": {"projectId": "10001"}}}, tmp_path=tmp_path)
    assert resolve_project_id(ctx, "P") == "10001"
    assert resolve_project_id(ctx, "P") == "10001"
    assert ctx.xray.calls_of("GetProjectSettings") == [{"projectIdOrKey": "P"}]
    assert ctx.xray.calls_of("GetProjectId") == []


def test_resolve_project_id_accepts_list_shape(tmp_path):
    ctx = make_context(responses={
        "GetProjectSettings": {"getProjectSettings": [{"projectId": ["10002"]}]}},
        tmp_path=tmp_path)
    assert resolve_project_id(ctx, "P") == "10002"


def test_resolve_project_id_falls_back_to_tests(tmp_path):
    def unsupported(_):
        raise ToolError("Cannot query field")
    ctx = make_context(responses={
        "GetProjectSettings": unsupported,
        "GetProjectId": {"getTests": {"results": [{"projectId": "10001"}]}}}, tmp_path=tmp_path)
    assert resolve_project_id(ctx, "P") == "10001"
    calls = ctx.xray.calls_of("GetProjectId")
    assert len(calls) == 1 and "project = " in calls[0]["jql"] and "P" in calls[0]["jql"]


def test_resolve_project_id_unresolvable(tmp_path):
    ctx = make_context(responses={"GetProjectSettings": {"getProjectSettings": None},
                                  "GetProjectId": {"getTests": {"results": []}}},
                       tmp_path=tmp_path)
    with pytest.raises(ToolError, match="numeric Xray project id"):
        resolve_project_id(ctx, "P")


def test_compact():
    issue = {"key": "P-1", "summary": "s", "status": "Open", "labels": ["a"], "description": "d",
             "testType": "Manual"}
    assert compact(issue, True) is issue
    assert compact(issue, False) == {"key": "P-1", "summary": "s", "status": "Open",
                                     "testType": "Manual", "labels": ["a"]}
    assert compact({"key": "P-2", "summary": "x", "other": 1}, False) == {"key": "P-2", "summary": "x"}


@pytest.mark.parametrize("bad", ["P OR project = Q", "p", "1P", "", "P\n", "P-1", 'P"'])
def test_require_project_rejects_malformed_key_before_settings(tmp_path, bad):
    ctx = make_context(tmp_path=tmp_path)
    for write in (True, False):
        with pytest.raises(ToolError, match="Invalid project key") as ei:
            require_project(ctx, bad, write=write)
        assert ei.value.data["projectKey"] == bad
