from __future__ import annotations

import json

import pytest

from conftest import FakeXray
from xray_cloud import approval
from xray_cloud.config import Credentials, SettingsStore
from xray_cloud.context import NO_CREDENTIALS_MESSAGE, ServerContext, require_project
from xray_cloud.errors import NeedsAnswer, ToolError
from xray_cloud.registry import Registry

SETTINGS = "projects:\n  PROJ: {}\n"
FOUND = {"GetProjectSettings": {"getProjectSettings": {"projectId": "100"}}}
NOT_FOUND = {"GetProjectSettings": {"getProjectSettings": None},
             "GetProjectId": {"getTests": {"results": []}}}


class FakeChannel:
    def __init__(self, answer=None, supports=True):
        self.answer = answer
        self.supports_elicitation = supports
        self.asked = []

    def elicit(self, message, schema):
        self.asked.append((message, schema))
        return self.answer


def make(tmp_path, settings=None, responses=None, read_only=False, creds=True, channel=None):
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path), "HOME": str(tmp_path / "home"),
           "XRAY_DATA_DIR": str(tmp_path / "data")}
    if creds:
        env.update(XRAY_CLIENT_ID="client-a", XRAY_CLIENT_SECRET="secret")
    if settings is not None:
        f = tmp_path / "settings.yml"
        f.write_text(settings)
        env["XRAY_SETTINGS_FILE"] = str(f)
    c = Credentials("client-a", "secret", "global", "https://x") if creds else None
    ctx = ServerContext(env, SettingsStore(env), c, read_only=read_only,
                        xray=FakeXray(responses if responses is not None else FOUND))
    ctx.channel = channel
    return ctx


def approvals(ctx):
    return ctx.preferences.approval("global", ctx.site)


def nothing_remembered(ctx):
    return approvals(ctx) == {"approveAll": False, "approvedProjects": []}


def test_read_only_blocks_even_with_approve_all(tmp_path):
    ctx = make(tmp_path, read_only=True)
    ctx.preferences.approve("global", ctx.site, None, True)
    with pytest.raises(ToolError, match="read-only"):
        approval.ensure_write_allowed(ctx, "OTHER", {})
    with pytest.raises(ToolError, match="read-only"):
        approval.ensure_write_allowed(ctx, "OTHER", {"allowProject": "all"})


def test_listed_project_allowed_without_question(tmp_path):
    ch = FakeChannel("no")
    ctx = make(tmp_path, settings=SETTINGS, channel=ch)
    approval.ensure_write_allowed(ctx, "PROJ", {})
    assert require_project(ctx, "PROJ", write=True).key == "PROJ"
    assert ch.asked == [] and nothing_remembered(ctx)


def test_approved_project_and_approve_all_allowed(tmp_path):
    ch = FakeChannel(None)
    ctx = make(tmp_path, channel=ch)
    ctx.preferences.approve("global", ctx.site, "PROJ", False)
    approval.ensure_write_allowed(ctx, "PROJ", {})
    with pytest.raises(ToolError):
        approval.ensure_write_allowed(ctx, "OTHER", {})
    ctx.preferences.approve("global", ctx.site, None, True)
    ch.asked.clear()
    approval.ensure_write_allowed(ctx, "OTHER", {})
    ps = require_project(ctx, "OTHER", write=True)
    assert ps.key == "OTHER" and ps.fields == {}
    assert ch.asked == []


def test_approve_all_on_one_site_does_not_cover_another(tmp_path):
    ctx = make(tmp_path)
    ctx.preferences.approve("global", ctx.site, None, True)
    ctx.env["XRAY_CLIENT_ID"] = "client-b"
    with pytest.raises(NeedsAnswer):
        approval.ensure_write_allowed(ctx, "PROJ", {})


def test_unapproved_with_elicitation_asks_client_and_remembers_all(tmp_path):
    ch = FakeChannel({"approve": "all"})
    ctx = make(tmp_path, channel=ch)
    approval.ensure_write_allowed(ctx, "PROJ", {})
    assert approvals(ctx) == {"approveAll": True, "approvedProjects": []}
    message, schema = ch.asked[0]
    assert "PROJ" in message and "global" in message
    prop = schema["properties"]["approve"]
    assert schema["type"] == "object" and schema["required"] == ["approve"]
    assert prop["type"] == "string" and prop["enum"] == ["all", "project", "no"]
    assert prop["default"] == "all" and prop["description"]
    assert ctx.xray.calls == []  # "all" needs no existence check
    approval.ensure_write_allowed(ctx, "OTHER", {})
    assert len(ch.asked) == 1


def test_unapproved_with_elicitation_asks_client_and_remembers_project(tmp_path):
    ch = FakeChannel({"approve": "project"})
    ctx = make(tmp_path, channel=ch)
    approval.ensure_write_allowed(ctx, "PROJ", {})
    assert approvals(ctx) == {"approveAll": False, "approvedProjects": ["PROJ"]}
    assert ctx.xray.calls_of("GetProjectSettings") == [{"projectIdOrKey": "PROJ"}]


@pytest.mark.parametrize("answer", [None, {"approve": "no"}, {"approve": "maybe"}, {}])
def test_elicitation_decline_writes_and_remembers_nothing(tmp_path, answer):
    ch = FakeChannel(answer)
    ctx = make(tmp_path, channel=ch)
    with pytest.raises(ToolError) as ei:
        approval.ensure_write_allowed(ctx, "PROJ", {"allowProject": "all"})
    assert not isinstance(ei.value, NeedsAnswer)
    assert "PROJ" in ei.value.message and "nothing was written" in ei.value.message.lower()
    assert len(ch.asked) == 1 and nothing_remembered(ctx)
    assert not ctx.preferences.path.exists()


def test_agent_allow_project_ignored_when_client_supports_elicitation(tmp_path):
    ch = FakeChannel({"approve": "project"})
    ctx = make(tmp_path, channel=ch)
    approval.ensure_write_allowed(ctx, "PROJ", {"allowProject": "all"})
    assert len(ch.asked) == 1
    assert approvals(ctx) == {"approveAll": False, "approvedProjects": ["PROJ"]}


def test_no_elicitation_returns_needs_answer_then_allow_project_all_works(tmp_path):
    for channel in (None, FakeChannel(supports=False)):
        sub = tmp_path / ("c%d" % id(channel))
        sub.mkdir()
        ctx = make(sub, channel=channel)
        with pytest.raises(NeedsAnswer) as ei:
            approval.ensure_write_allowed(ctx, "PROJ", {})
        result = ei.value.to_dict()
        assert result["status"] == "needs_answer" and "error" not in result
        (q,) = result["questions"]
        assert q["id"] == "allowProject" and q["choices"] == ["all", "project"]
        assert q["default"] == "all" and "PROJ" in q["question"]
        assert nothing_remembered(ctx)
        approval.ensure_write_allowed(ctx, "PROJ", {"allowProject": "all"})
        assert approvals(ctx)["approveAll"] is True
        approval.ensure_write_allowed(ctx, "OTHER", {})
        if channel is not None:
            assert channel.asked == []


def test_no_elicitation_allow_project_project_checks_and_remembers(tmp_path):
    ctx = make(tmp_path)
    approval.ensure_write_allowed(ctx, "PROJ", {"allowProject": "project"})
    assert approvals(ctx) == {"approveAll": False, "approvedProjects": ["PROJ"]}
    with pytest.raises(NeedsAnswer):
        approval.ensure_write_allowed(ctx, "OTHER", {})


@pytest.mark.parametrize("channel", [None, FakeChannel({"approve": "project"})])
def test_unknown_project_refused_not_remembered(tmp_path, channel):
    ctx = make(tmp_path, responses=NOT_FOUND, channel=channel)
    with pytest.raises(ToolError) as ei:
        approval.ensure_write_allowed(ctx, "NOPE", {"allowProject": "project"})
    assert not isinstance(ei.value, NeedsAnswer)
    assert "NOPE" in ei.value.message and "not found" in ei.value.message
    assert nothing_remembered(ctx)


def test_existence_check_error_refused_not_remembered(tmp_path):
    def boom(_variables):
        raise ToolError("Xray request failed", status=500)
    ctx = make(tmp_path, responses={"GetProjectSettings": boom, "GetProjectId": boom},
               channel=FakeChannel({"approve": "project"}))
    with pytest.raises(ToolError) as ei:
        approval.ensure_write_allowed(ctx, "PROJ", {})
    assert "PROJ" in ei.value.message and "Xray request failed" in ei.value.message
    assert nothing_remembered(ctx)


def test_no_credentials_refused_with_existing_message(tmp_path):
    ctx = make(tmp_path, creds=False, channel=FakeChannel({"approve": "all"}))
    with pytest.raises(ToolError) as ei:
        approval.ensure_write_allowed(ctx, "PROJ", {"allowProject": "all"})
    assert ei.value.message == NO_CREDENTIALS_MESSAGE
    assert ctx.channel.asked == []


def test_settings_file_without_projects_is_valid(tmp_path):
    ctx = make(tmp_path, settings="duplicateCheck: false\n")
    settings = ctx.settings_store.get()
    assert settings.path is not None and settings.projects == {}
    with pytest.raises(NeedsAnswer):
        require_project(ctx, "PROJ", write=True)
    ctx.preferences.approve("global", ctx.site, "PROJ", False)
    assert require_project(ctx, "PROJ", write=True).key == "PROJ"


def test_no_settings_file_write_asks_instead_of_error(tmp_path):
    ctx = make(tmp_path)
    with pytest.raises(NeedsAnswer):
        require_project(ctx, "PROJ", write=True)


def _registry():
    reg = Registry()

    @reg.tool("write_thing", "Writes.", {"type": "object", "properties": {
        "projectKey": {"type": "string"}}, "required": ["projectKey"]}, write=True)
    def write_thing(ctx, args):
        require_project(ctx, args["projectKey"], write=True)
        return {"ok": True, "args": sorted(args)}

    @reg.tool("read_thing", "Reads.", {"type": "object", "properties": {
        "projectKey": {"type": "string"}}}, write=False)
    def read_thing(ctx, args):
        return {"ok": True}
    return reg


def _call(reg, ctx, name, **args):
    res = reg.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def test_registry_adds_allow_project_to_write_tools_only():
    reg = _registry()
    schemas = {t["name"]: t["inputSchema"] for t in reg.list()}
    prop = schemas["write_thing"]["properties"]["allowProject"]
    assert prop["type"] == "string" and prop["enum"] == ["all", "project"]
    assert prop["description"]
    assert "allowProject" not in schemas["write_thing"].get("required", [])
    assert "allowProject" not in schemas["read_thing"]["properties"]


def test_registry_needs_answer_then_allow_project(tmp_path):
    reg, ctx = _registry(), make(tmp_path)
    res, err = _call(reg, ctx, "write_thing", projectKey="PROJ")
    assert not err and res["status"] == "needs_answer"
    assert res["questions"][0]["id"] == "allowProject"
    res, err = _call(reg, ctx, "write_thing", projectKey="PROJ", allowProject="all")
    assert not err and res == {"ok": True, "args": ["projectKey"]}
    res, err = _call(reg, ctx, "write_thing", projectKey="OTHER")
    assert not err and res["ok"] is True


def test_registry_agent_allow_project_ignored_with_elicitation(tmp_path):
    ch = FakeChannel(None)
    reg, ctx = _registry(), make(tmp_path, channel=ch)
    res, err = _call(reg, ctx, "write_thing", projectKey="PROJ", allowProject="all")
    assert err and "PROJ" in res["error"] and len(ch.asked) == 1
    assert nothing_remembered(ctx)


def test_reads_allowed_when_listed_approved_or_no_projects_declared(tmp_path):
    ctx = make(tmp_path, settings=SETTINGS)
    assert require_project(ctx, "PROJ", write=False).key == "PROJ"
    with pytest.raises(ToolError, match="Unknown Xray project key") as ei:
        require_project(ctx, "OTHER", write=False)
    assert ei.value.data["known"] == ["PROJ"] and "approval" in ei.value.data["hint"]
    ctx.preferences.approve("global", ctx.site, "OTHER", False)
    ps = require_project(ctx, "OTHER", write=False)
    assert ps.key == "OTHER" and ps.fields == {}
    with pytest.raises(ToolError, match="Unknown Xray project key"):
        require_project(ctx, "THIRD", write=False)
    ctx.preferences.approve("global", ctx.site, None, True)
    assert require_project(ctx, "THIRD", write=False).key == "THIRD"


def test_reads_allowed_when_file_declares_no_projects_or_is_missing(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ctx = make(tmp_path / "a", settings="duplicateCheck: false\n")
    assert require_project(ctx, "ANY", write=False).key == "ANY"
    ctx = make(tmp_path / "b")
    assert require_project(ctx, "ANY", write=False).key == "ANY"


def test_reads_of_unlisted_project_without_credentials_still_unknown(tmp_path):
    ctx = make(tmp_path, settings=SETTINGS, creds=False)
    with pytest.raises(ToolError, match="Unknown Xray project key"):
        require_project(ctx, "OTHER", write=False)


def test_project_not_found_detected_by_reason_not_message(tmp_path):
    from xray_cloud.context import resolve_project_id
    ctx = make(tmp_path, responses=NOT_FOUND)
    with pytest.raises(ToolError) as ei:
        resolve_project_id(ctx, "NOPE")
    assert ei.value.data["reason"] == "project_not_found"
