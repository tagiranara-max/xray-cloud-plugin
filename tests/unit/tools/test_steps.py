from __future__ import annotations

import base64
import json
import re

import pytest

from conftest import make_context
from xray_cloud.errors import ToolError
from xray_cloud.registry import REGISTRY
from xray_cloud.stepcache import CachedTest, StepCache
import xray_cloud.tools  # noqa: F401

Y = "projects:\n  P: {}\n"
LINK = "https://xray.cloud.getxray.app/api/v2/attachments/"
MUTATIONS = ("AddTestStep", "RemoveTestStep", "UpdateTestStep")


@pytest.fixture(autouse=True)
def _recovery_in_tmp(tmp_path, monkeypatch):
    """Keep recovery directories created by partial-failure tests out of the system temp dir."""
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


class StepServer:
    """Stateful fake of the Xray step API, installed on a FakeXray. Records an event log
    (operation names and downloads, in order) and can inject failures."""

    def __init__(self, steps=None, kind="Steps"):
        self.n = 0
        self.tests = {"P-1": {"issueId": "100", "kind": kind, "gherkin": None,
                              "steps": [self._mk(**s) for s in (steps or [])]}}
        self.log = []
        self.fail = {}  # op -> 1-based call number that raises
        self.counts = {}
        self.get_fails = False
        self.fail_message = None  # error text for injected failures (default "boom in <op>")

    def _id(self, prefix):
        self.n += 1
        return "%s%d" % (prefix, self.n)

    def _mk(self, action, result="r", data=None, attachments=(), callTestIssueId=None):
        return {"id": self._id("st"), "action": action, "data": data, "result": result,
                "attachments": [dict(a) for a in attachments], "callTestIssueId": callTestIssueId}

    def steps(self, key="P-1"):
        return self.tests[key]["steps"]

    def _tick(self, op):
        self.log.append(op)
        self.counts[op] = self.counts.get(op, 0) + 1
        if self.fail.get(op) == self.counts[op]:
            raise ToolError(self.fail_message or "boom in %s" % op)

    def _by_issue_id(self, issue_id):
        return next(t for t in self.tests.values() if t["issueId"] == issue_id)

    def get(self, v):
        self.log.append("GetTestSteps")
        if self.get_fails:
            raise ToolError("get failed")
        key = re.search(r"issue = (\S+)", v["jql"]).group(1)
        t = self.tests.get(key)
        if t is None:
            return {"getTests": {"results": []}}
        return {"getTests": {"results": [{
            "issueId": t["issueId"], "testType": {"name": "Manual", "kind": t["kind"]},
            "gherkin": t["gherkin"], "folder": {"path": "/F"},
            "steps": [dict(s) for s in t["steps"]], "jira": {"key": key}}]}}

    def called(self, v):
        self.log.append("GetCalledTests")
        out = []
        for key, t in self.tests.items():
            if t["issueId"] in v["issueIds"]:
                out.append({"issueId": t["issueId"], "steps": t["steps"],
                            "jira": {"key": key, "summary": "called " + key}})
        return {"getTests": {"results": out}}

    def add(self, v):
        self._tick("AddTestStep")
        s = v["step"]
        atts = []
        for a in s.get("attachments") or []:
            aid = self._id("att")
            atts.append({"id": aid, "filename": a["filename"], "downloadLink": LINK + aid,
                         "_data": a["data"], "_mime": a["mimeType"]})
        step = {"id": self._id("st"), "action": s["action"], "data": s.get("data"),
                "result": s["result"], "attachments": atts, "callTestIssueId": None}
        self._by_issue_id(v["issueId"])["steps"].append(step)
        return {"addTestStep": {"id": step["id"], "action": step["action"], "data": step["data"],
                                "result": step["result"],
                                "attachments": [{"id": a["id"], "filename": a["filename"]}
                                                for a in atts]}}

    def _find(self, step_id):
        for t in self.tests.values():
            for i, s in enumerate(t["steps"]):
                if s["id"] == step_id:
                    return t, i
        raise ToolError("no step " + step_id)

    def remove(self, v):
        self._tick("RemoveTestStep")
        t, i = self._find(v["stepId"])
        del t["steps"][i]
        return {"removeTestStep": "ok"}

    def update(self, v):
        self._tick("UpdateTestStep")
        t, i = self._find(v["stepId"])
        step = t["steps"][i]
        for k in ("action", "data", "result"):
            if k in v["step"]:
                step[k] = v["step"][k]
        for a in (v["step"].get("attachments") or {}).get("add", []):
            aid = self._id("att")
            step["attachments"].append({"id": aid, "filename": a["filename"],
                                        "downloadLink": LINK + aid, "_data": a["data"]})
        return {"updateTestStep": {"__typename": "Step"}}

    def install(self, ctx):
        fx = ctx.xray
        fx.responses.update({"GetTestSteps": self.get, "GetCalledTests": self.called,
                             "AddTestStep": self.add, "RemoveTestStep": self.remove,
                             "UpdateTestStep": self.update})
        orig = fx.download

        def download(url):
            self.log.append("download")
            return orig(url)
        fx.download = download
        return ctx

    def mutations(self):
        return [e for e in self.log if e in MUTATIONS]


def actions(steps):
    return [s["action"] for s in steps]


def setup(steps=("s1", "s2", "s3", "s4"), read_only=False, kind="Steps"):
    server = StepServer([{"action": a} if isinstance(a, str) else a for a in steps], kind=kind)
    ctx = server.install(make_context(Y, read_only=read_only))
    return ctx, server


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


# -- get_test_steps ------------------------------------------------------------

def test_get_steps_returns_compact_list_and_caches():
    ctx, server = setup([{"action": "s1", "data": "d", "result": "r1",
                          "attachments": [{"id": "a", "filename": "x.png", "downloadLink": "l"}]},
                         "s2"])
    res, err = call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    assert not err
    assert res["steps"] == [
        {"n": 1, "action": "s1", "data": "d", "result": "r1", "attachments": ["x.png"]},
        {"n": 2, "action": "s2", "data": None, "result": "r", "attachments": []}]
    assert res["fromCache"] is False and res["folderPath"] == "/F"
    res2, _ = call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    assert res2["fromCache"] is True and "cachedAt" in res2
    assert server.log.count("GetTestSteps") == 1
    res3, _ = call(ctx, "get_test_steps", projectKey="P", testKey="P-1", forceRefresh=True)
    assert res3["fromCache"] is False and server.log.count("GetTestSteps") == 2


def test_get_steps_of_gherkin_test_returns_gherkin():
    ctx, server = setup([], kind="Gherkin")
    server.tests["P-1"]["gherkin"] = "Scenario: x"
    res, _ = call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    assert res["gherkin"] == "Scenario: x" and "steps" not in res


def test_get_steps_expands_call_test_steps():
    ctx, server = setup(["s1", {"action": "", "callTestIssueId": "200"}])
    server.tests["P-2"] = {"issueId": "200", "kind": "Steps", "gherkin": None,
                           "steps": [server._mk("sub1")]}
    res, _ = call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    called = res["steps"][1]
    assert called["n"] == 2 and called["calledTest"] == "P-2"
    assert called["calledTestSummary"] == "called P-2"
    assert [s["action"] for s in called["calledSteps"]] == ["sub1"]


def test_get_steps_rejects_other_project_issue():
    ctx, server = setup()
    res, err = call(ctx, "get_test_steps", projectKey="P", testKey="Q-1")
    assert err and "does not belong" in res["error"] and server.log == []


def test_get_steps_test_not_found():
    ctx, _ = setup()
    res, err = call(ctx, "get_test_steps", projectKey="P", testKey="P-9")
    assert err and "P-9" in res["error"]


# -- add_test_steps ------------------------------------------------------------

def test_add_at_end_appends_without_removing():
    ctx, server = setup()
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}])
    assert not err
    assert actions(server.steps()) == ["s1", "s2", "s3", "s4", "n1"]
    assert "RemoveTestStep" not in server.log
    assert actions(res["steps"]) == ["s1", "s2", "s3", "s4", "n1"]
    assert res["added"] == [5]


def test_add_step_result_is_optional_like_create_test():
    ctx, server = setup()
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "only action"}])
    assert not err, res
    assert ctx.xray.calls_of("AddTestStep")[0]["step"]["result"] == ""
    from xray_cloud.registry import REGISTRY as R
    add = R.get("add_test_steps").params["properties"]["steps"]["items"]
    create = R.get("create_test").params["properties"]["steps"]["items"]
    assert add["required"] == create["required"] == ["action"]


def test_insert_after_step_2_rebuilds_tail_in_order():
    ctx, server = setup()
    old_ids = [s["id"] for s in server.steps()]
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}, {"action": "n2", "result": "r"}],
                    position={"at": "after", "stepNumber": 2})
    assert not err, res
    assert actions(server.steps()) == ["s1", "s2", "n1", "n2", "s3", "s4"]
    assert [s["id"] for s in server.steps()[:2]] == old_ids[:2]  # head untouched
    assert server.log.count("RemoveTestStep") == 2
    assert res["added"] == [3, 4]
    assert [s["n"] for s in res["steps"]] == [1, 2, 3, 4, 5, 6]


def test_insert_at_start():
    ctx, server = setup(["s1", "s2"])
    _, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                  steps=[{"action": "n1", "result": "r"}], position={"at": "start"})
    assert not err
    assert actions(server.steps()) == ["n1", "s1", "s2"]


def test_insert_preserves_data_and_result_of_tail():
    ctx, server = setup(["s1", {"action": "s2", "data": "D2", "result": "R2"}])
    call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
         steps=[{"action": "n", "result": "r"}], position={"at": "after", "stepNumber": 1})
    last = server.steps()[-1]
    assert (last["action"], last["data"], last["result"]) == ("s2", "D2", "R2")


def test_insert_after_out_of_range_is_error_without_mutation():
    ctx, server = setup()
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "after", "stepNumber": 5})
    assert err and "1" in res["error"] and "4" in res["error"]
    assert server.mutations() == []


def test_after_requires_step_number():
    ctx, server = setup()
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "after"})
    assert err and "stepNumber" in res["error"] and server.mutations() == []


def test_blank_action_rejected():
    ctx, server = setup()
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "  ", "result": "r"}])
    assert err and "action" in res["error"] and server.log == []


def test_tail_attachments_downloaded_before_any_mutation():
    ctx, server = setup(["s1", {"action": "s2", "attachments": [
        {"id": "old1", "filename": "shot.png", "downloadLink": LINK + "old1"}]}])
    ctx.xray.downloads[LINK + "old1"] = b"PNGBYTES"
    _, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                  steps=[{"action": "n", "result": "r"}], position={"at": "start"})
    assert not err
    first_mut = min(server.log.index(m) for m in MUTATIONS if m in server.log)
    assert server.log.index("download") < first_mut
    readded = server.steps()[-1]
    assert readded["action"] == "s2"
    assert readded["attachments"][0]["filename"] == "shot.png"
    assert base64.b64decode(readded["attachments"][0]["_data"]) == b"PNGBYTES"
    assert readded["attachments"][0]["_mime"] == "image/png"


def test_download_failure_aborts_before_any_mutation():
    ctx, server = setup(["s1", {"action": "s2", "attachments": [
        {"id": "old1", "filename": "shot.png", "downloadLink": LINK + "old1"}]}])
    ctx.xray.downloads[LINK + "old1"] = ToolError("Xray request failed with HTTP 500: x")
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "start"})
    assert err and "500" in res["error"]
    assert server.mutations() == []
    assert actions(server.steps()) == ["s1", "s2"]


def test_inline_attachment_markup_rewritten_to_new_ids():
    ctx, server = setup(["s1", {"action": "see !xray-attachment://old1|width=10!",
                                "attachments": [{"id": "old1", "filename": "a.png",
                                                 "downloadLink": LINK + "old1"}]}])
    ctx.xray.downloads[LINK + "old1"] = b"x"
    _, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                  steps=[{"action": "n", "result": "r"}], position={"at": "after", "stepNumber": 1})
    assert not err
    readded = server.steps()[-1]
    new_id = readded["attachments"][0]["id"]
    assert readded["action"] == "see !xray-attachment://%s|width=10!" % new_id


def test_call_test_step_in_tail_refuses_with_reason():
    ctx, server = setup(["s1", {"action": "", "callTestIssueId": "200"}, "s3"])
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "start"})
    assert err and "call-test" in res["error"] and "step 2" in res["error"]
    assert server.mutations() == []


def test_call_test_step_before_insertion_point_is_fine():
    ctx, server = setup([{"action": "", "callTestIssueId": "200"}, "s2"])
    _, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                  steps=[{"action": "n", "result": "r"}], position={"at": "after", "stepNumber": 1})
    assert not err
    assert actions(server.steps()) == ["", "n", "s2"]


def test_missing_local_attachment_rejected_before_any_mutation(tmp_path):
    ctx, server = setup()
    missing = str(tmp_path / "nope.png")
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r", "attachments": [missing]}],
                    position={"at": "start"})
    assert err and missing in res["error"]
    assert server.mutations() == []


def test_local_attachment_uploaded(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    ctx, server = setup(["s1"])
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r", "attachments": [str(f)]}])
    assert not err
    att = server.steps()[-1]["attachments"][0]
    assert att["filename"] == "doc.pdf" and att["_mime"] == "application/pdf"
    assert base64.b64decode(att["_data"]) == b"%PDF"
    assert res["steps"][-1]["attachments"] == ["doc.pdf"]


def test_add_refused_on_gherkin_test():
    ctx, server = setup([], kind="Gherkin")
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}])
    assert err and "Gherkin" in res["error"] and server.mutations() == []


def test_write_tools_blocked_in_read_only_mode():
    ctx, server = setup(read_only=True)
    for name, extra in [("add_test_steps", {"steps": [{"action": "n", "result": "r"}]}),
                        ("edit_test_step", {"stepNumber": 1, "action": "x"}),
                        ("remove_test_step", {"stepNumber": 1}),
                        ("move_test_step", {"fromStep": 1, "toStep": 2})]:
        res, err = call(ctx, name, projectKey="P", testKey="P-1", **extra)
        assert err and "read-only" in res["error"]
    assert server.log == []


# -- partial failure -----------------------------------------------------------

def test_failure_mid_rebuild_reports_state():
    ctx, server = setup()
    server.fail["AddTestStep"] = 2  # new step added, then re-adding s3 fails
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}], position={"at": "after", "stepNumber": 2})
    assert err
    assert "boom in AddTestStep" in res["cause"]
    assert actions(res["currentSteps"]) == ["s1", "s2", "n1"]
    assert [(s["originalStep"], s["action"]) for s in res["notReAdded"]] == [(3, "s3"), (4, "s4")]
    assert res["notAdded"] == []
    assert "P-1" in res["error"]


def test_rebuild_failure_from_missing_permission_is_flagged():
    ctx, server = setup()
    server.fail["AddTestStep"] = 2
    server.fail_message = "You do not have permission to edit issues in this project"
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}], position={"at": "after", "stepNumber": 2})
    assert err and res["reason"] == "permission_denied" and "notReAdded" in res


def test_failure_while_removing_tail_reports_new_steps_not_added():
    ctx, server = setup()
    server.fail["RemoveTestStep"] = 2  # s3 removed, removing s4 fails
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}], position={"at": "after", "stepNumber": 2})
    assert err
    assert actions(res["currentSteps"]) == ["s1", "s2", "s4"]
    assert [s["originalStep"] for s in res["notReAdded"]] == [3]  # s4 still exists
    assert [s["action"] for s in res["notAdded"]] == ["n1"]


def test_failure_report_when_refetch_also_fails():
    ctx, server = setup()
    server.fail["AddTestStep"] = 1

    orig = server.get

    def get(v):
        if "AddTestStep" in server.log:
            server.get_fails = True
        return orig(v)
    ctx.xray.responses["GetTestSteps"] = get
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}], position={"at": "start"})
    assert err and res["currentSteps"] is None and "get failed" in res["currentStepsError"]
    assert len(res["notReAdded"]) == 4


def test_step_cache_invalidated_after_mutation():
    ctx, server = setup(["s1"])
    call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    call(ctx, "add_test_steps", projectKey="P", testKey="P-1", steps=[{"action": "n", "result": "r"}])
    res, _ = call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    assert actions(res["steps"]) == ["s1", "n"]


def test_step_cache_invalidated_even_when_mutation_fails():
    ctx, server = setup(["s1"])
    call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    server.fail["AddTestStep"] = 2
    call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
         steps=[{"action": "n1", "result": "r"}, {"action": "n2", "result": "r"}])
    res, _ = call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    assert res["fromCache"] is False or actions(res["steps"]) == ["s1", "n1"]
    assert actions(res["steps"]) == ["s1", "n1"]


def test_mutations_read_fresh_steps_not_cache():
    ctx, server = setup(["s1", "s2"])
    call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
    server.tests["P-1"]["steps"].append(server._mk("external"))  # changed outside the plugin
    call(ctx, "remove_test_step", projectKey="P", testKey="P-1", stepNumber=3)
    assert actions(server.steps()) == ["s1", "s2"]


# -- move_test_step ------------------------------------------------------------

def test_move_step_4_to_1_gives_order_4_1_2_3():
    ctx, server = setup()
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=4, toStep=1)
    assert not err, res
    assert actions(server.steps()) == ["s4", "s1", "s2", "s3"]
    assert actions(res["steps"]) == ["s4", "s1", "s2", "s3"]


def test_move_step_1_to_3_only_rebuilds_from_step_1():
    ctx, server = setup()
    call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=1, toStep=3)
    assert actions(server.steps()) == ["s2", "s3", "s1", "s4"]


def test_move_step_3_to_4_keeps_head():
    ctx, server = setup()
    head = [s["id"] for s in server.steps()[:2]]
    call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=3, toStep=4)
    assert actions(server.steps()) == ["s1", "s2", "s4", "s3"]
    assert [s["id"] for s in server.steps()[:2]] == head
    assert server.log.count("RemoveTestStep") == 2


def test_move_out_of_range_is_error():
    ctx, server = setup()
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=5, toStep=1)
    assert err and "1-4" in res["error"] and res["validRange"] == [1, 4]
    assert server.mutations() == []


def test_move_same_position_is_noop_without_calls():
    ctx, server = setup()
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=2, toStep=2)
    assert not err and actions(res["steps"]) == ["s1", "s2", "s3", "s4"]
    assert server.mutations() == []


def test_move_refuses_call_test_in_tail():
    ctx, server = setup(["s1", {"action": "", "callTestIssueId": "200"}, "s3"])
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=3, toStep=1)
    assert err and "call-test" in res["error"] and server.mutations() == []


def test_move_failure_reports_state():
    ctx, server = setup()
    server.fail["AddTestStep"] = 2
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=4, toStep=1)
    assert err
    assert actions(res["currentSteps"]) == ["s4"]
    assert [s["originalStep"] for s in res["notReAdded"]] == [1, 2, 3]


# -- edit / remove -------------------------------------------------------------

def test_edit_step_updates_fields():
    ctx, server = setup()
    res, err = call(ctx, "edit_test_step", projectKey="P", testKey="P-1", stepNumber=2,
                    action="new", data="d")
    assert not err
    s = server.steps()[1]
    assert (s["action"], s["data"], s["result"]) == ("new", "d", "r")
    assert res["updatedStep"] == 2 and actions(res["steps"])[1] == "new"


def test_edit_requires_some_field():
    ctx, server = setup()
    res, err = call(ctx, "edit_test_step", projectKey="P", testKey="P-1", stepNumber=1)
    assert err and server.log == []


def test_edit_out_of_range():
    ctx, server = setup()
    res, err = call(ctx, "edit_test_step", projectKey="P", testKey="P-1", stepNumber=9, action="x")
    assert err and "1-4" in res["error"] and server.mutations() == []


def test_edit_call_test_step_refused():
    ctx, server = setup(["s1", {"action": "", "callTestIssueId": "200"}])
    res, err = call(ctx, "edit_test_step", projectKey="P", testKey="P-1", stepNumber=2, action="x")
    assert err and "call-test" in res["error"] and server.mutations() == []


def test_edit_with_attachment_and_embed(tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"img")
    ctx, server = setup()
    res, err = call(ctx, "edit_test_step", projectKey="P", testKey="P-1", stepNumber=1,
                    attachments=[str(f)], embedAttachmentsIn="result")
    assert not err, res
    s = server.steps()[0]
    att = s["attachments"][0]
    assert att["filename"] == "pic.png" and base64.b64decode(att["_data"]) == b"img"
    assert s["result"] == "r\n\n!xray-attachment://%s|width=260,height=270!" % att["id"]


def test_edit_embed_requires_attachments():
    ctx, server = setup()
    res, err = call(ctx, "edit_test_step", projectKey="P", testKey="P-1", stepNumber=1,
                    action="x", embedAttachmentsIn="result")
    assert err and "attachments" in res["error"] and server.mutations() == []


def test_edit_missing_attachment_rejected_before_mutation(tmp_path):
    ctx, server = setup()
    missing = str(tmp_path / "gone.png")
    res, err = call(ctx, "edit_test_step", projectKey="P", testKey="P-1", stepNumber=1,
                    action="x", attachments=[missing])
    assert err and missing in res["error"] and server.mutations() == []


def test_remove_step():
    ctx, server = setup()
    res, err = call(ctx, "remove_test_step", projectKey="P", testKey="P-1", stepNumber=2)
    assert not err
    assert actions(server.steps()) == ["s1", "s3", "s4"]
    assert res["removed"] == {"n": 2, "action": "s2", "data": None, "result": "r",
                              "attachments": []}
    assert actions(res["steps"]) == ["s1", "s3", "s4"]


def test_remove_out_of_range():
    ctx, server = setup()
    res, err = call(ctx, "remove_test_step", projectKey="P", testKey="P-1", stepNumber=0)
    assert err and server.mutations() == []


# -- StepCache -----------------------------------------------------------------

def test_step_cache_ttl_expiry():
    now = [0.0]
    cache = StepCache(ttl_s=10, clock=lambda: now[0])
    cache.set("P-1", CachedTest(key="P-1", issue_id="1"))
    assert cache.get("P-1") is not None
    now[0] = 11
    assert cache.get("P-1") is None


def test_step_cache_invalidate():
    cache = StepCache()
    cache.set("P-1", CachedTest(key="P-1", issue_id="1"))
    cache.invalidate("P-1")
    assert cache.get("P-1") is None
    cache.invalidate("P-1")  # idempotent


def test_first_mutation_failing_cleanly_gives_plain_error():
    ctx, server = setup()
    server.fail["RemoveTestStep"] = 1
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}], position={"at": "start"})
    assert err and res["error"] == "boom in RemoveTestStep" and "currentSteps" not in res
    assert actions(server.steps()) == ["s1", "s2", "s3", "s4"]


def test_remove_that_took_effect_despite_error_is_reported_lost():
    ctx, server = setup()
    orig = server.remove

    def remove(v):
        orig(v)  # the deletion happens...
        raise ToolError("timeout")  # ...but the response is lost
    ctx.xray.responses["RemoveTestStep"] = remove
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=2, toStep=1)
    assert err and actions(res["currentSteps"]) == ["s2", "s3", "s4"]
    assert [s["originalStep"] for s in res["notReAdded"]] == [1]


def test_end_append_failure_after_some_added_reports_not_added():
    ctx, server = setup(["s1"])
    server.fail["AddTestStep"] = 2
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n1", "result": "r"}, {"action": "n2", "result": "r"}])
    assert err and actions(res["currentSteps"]) == ["s1", "n1"]
    assert [s["action"] for s in res["notAdded"]] == ["n2"] and res["notReAdded"] == []


def test_test_tools_invalidate_step_cache():
    ctx, server = setup(["s1"])
    ctx.xray.responses.update({
        "SearchTests": {"getTests": {"results": [{"issueId": "100", "jira": {"key": "P-1"}}]}},
        "DeleteTest": {"deleteTest": "ok"}, "UpdateTestType": {"updateTestType": {}},
        "UpdateGherkin": {"updateGherkinTestDefinition": {}}})
    for name, extra in [("delete_test", {"confirm": True}), ("update_test_type", {"testType": "Generic"}),
                        ("edit_test", {"gherkin": "Scenario: x"})]:
        call(ctx, "get_test_steps", projectKey="P", testKey="P-1")
        assert ctx.step_cache.get("P-1") is not None
        _, err = call(ctx, name, projectKey="P", testKey="P-1", **extra)
        assert not err and ctx.step_cache.get("P-1") is None, name



# -- review fixes: attachment loss ---------------------------------------------

def test_failure_saves_lost_attachments_to_recovery_dir():
    ctx, server = setup(["s1", {"action": "s2", "attachments": [
        {"id": "o1", "filename": "a.png", "downloadLink": LINK + "o1"},
        {"id": "o2", "filename": "b.pdf", "downloadLink": LINK + "o2"}]}])
    ctx.xray.downloads.update({LINK + "o1": b"AAA", LINK + "o2": b"BBB"})
    server.fail["AddTestStep"] = 2  # new step added, re-adding s1 fails
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "start"})
    assert err and res["recoveryDir"] in res["error"]
    lost = {s["originalStep"]: s for s in res["notReAdded"]}
    assert sorted(lost) == [1, 2] and lost[1]["attachments"] == []
    saved = lost[2]["attachments"]
    assert [a["filename"] for a in saved] == ["a.png", "b.pdf"]
    with open(saved[0]["savedTo"], "rb") as fh:
        assert fh.read() == b"AAA"
    with open(saved[1]["savedTo"], "rb") as fh:
        assert fh.read() == b"BBB"
    assert saved[0]["savedTo"].startswith(res["recoveryDir"])


def test_saved_attachment_paths_can_be_passed_back_to_add_test_steps():
    ctx, server = setup([{"action": "s1", "attachments": [
        {"id": "o1", "filename": "a.png", "downloadLink": LINK + "o1"}]}])
    ctx.xray.downloads[LINK + "o1"] = b"AAA"
    server.fail["AddTestStep"] = 2
    res, _ = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                  steps=[{"action": "n", "result": "r"}], position={"at": "start"})
    lost = res["notReAdded"][0]
    server.fail.clear()
    _, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1", steps=[{
        "action": lost["action"], "result": lost["result"],
        "attachments": [a["savedTo"] for a in lost["attachments"]]}])
    assert not err
    assert base64.b64decode(server.steps()[-1]["attachments"][0]["_data"]) == b"AAA"


def test_no_recovery_dir_when_lost_steps_have_no_attachments():
    ctx, server = setup()
    server.fail["AddTestStep"] = 2
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "start"})
    assert err and "recoveryDir" not in res


UNLINKED = ["s1", {"action": "s2", "attachments": [{"id": "o1", "filename": "nolink.png",
                                                     "downloadLink": None}]}]


def test_attachment_without_download_link_refused_before_mutation():
    ctx, server = setup(UNLINKED)
    for name, extra in [("add_test_steps", {"steps": [{"action": "n", "result": "r"}],
                                            "position": {"at": "start"}}),
                        ("move_test_step", {"fromStep": 2, "toStep": 1})]:
        res, err = call(ctx, name, projectKey="P", testKey="P-1", **extra)
        assert err and "nolink.png" in res["error"] and "step 2" in res["error"], name
        assert "allowAttachmentLoss" in res["error"]
    assert server.mutations() == []


def test_allow_attachment_loss_proceeds_with_warning():
    ctx, server = setup(UNLINKED)
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=2, toStep=1,
                    allowAttachmentLoss=True)
    assert not err and actions(server.steps()) == ["s2", "s1"]
    assert any("nolink.png" in w for w in res["warnings"])
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "start"},
                    allowAttachmentLoss=True)
    assert not err


def test_partial_failure_includes_warnings():
    ctx, server = setup(UNLINKED)
    server.fail["AddTestStep"] = 1
    res, err = call(ctx, "move_test_step", projectKey="P", testKey="P-1", fromStep=2, toStep=1,
                    allowAttachmentLoss=True)
    assert err and any("nolink.png" in w for w in res["warnings"])


def test_remove_call_test_step_echoes_call_reference():
    ctx, server = setup(["s1", {"action": "", "callTestIssueId": "200"}])
    res, err = call(ctx, "remove_test_step", projectKey="P", testKey="P-1", stepNumber=2)
    assert not err and res["removed"] == {"n": 2, "callTestIssueId": "200"}


# -- review fixes: recovery file naming ----------------------------------------

def _fail_with_attachments(filenames):
    """Step 1 carries attachments with the given names (content b"<index>"); the rebuild
    fails re-adding it, so all of them must be recovered."""
    atts = [{"id": "o%d" % i, "filename": f, "downloadLink": LINK + "o%d" % i}
            for i, f in enumerate(filenames)]
    ctx, server = setup([{"action": "s1", "attachments": atts}])
    for i in range(len(filenames)):
        ctx.xray.downloads[LINK + "o%d" % i] = str(i).encode()
    server.fail["AddTestStep"] = 2
    res, err = call(ctx, "add_test_steps", projectKey="P", testKey="P-1",
                    steps=[{"action": "n", "result": "r"}], position={"at": "start"})
    assert err and "notReAdded" in res, res
    return res, res["notReAdded"][0]["attachments"]


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def test_duplicate_filenames_do_not_overwrite_each_other():
    res, saved = _fail_with_attachments(["image.png", "image.png", "IMAGE.png"])
    paths = [a["savedTo"] for a in saved]
    assert len(set(p.lower() for p in paths)) == 3
    assert [_read(p) for p in paths] == [b"0", b"1", b"2"]
    assert paths[0].endswith("image.png") and paths[1].endswith("image-2.png")
    assert [a["filename"] for a in saved] == ["image.png", "image.png", "IMAGE.png"]


def test_traversal_and_degenerate_names_stay_inside_recovery_dir():
    import os
    res, saved = _fail_with_attachments(["../x", "a/b.png", "..\\\\c.txt", "..", ".", ""])
    root = os.path.realpath(res["recoveryDir"])
    for i, a in enumerate(saved):
        assert a["savedTo"], a
        assert os.path.realpath(a["savedTo"]).startswith(root + os.sep)
        assert _read(a["savedTo"]) == str(i).encode()
    names = [os.path.basename(a["savedTo"]) for a in saved]
    assert names[:3] == ["x", "b.png", "c.txt"]
    assert names[3:] == ["attachment-4", "attachment-5", "attachment-6"]


def test_unwritable_name_records_save_error_and_keeps_partial_failure():
    res, saved = _fail_with_attachments(["ok.png", "bad\x00.png"])
    assert "Rebuilding the steps of P-1 failed" in res["error"]
    assert _read(saved[0]["savedTo"]) == b"0"
    assert saved[1]["savedTo"] is None and saved[1]["saveError"]


def test_mkdtemp_failure_records_save_error_and_keeps_partial_failure(monkeypatch):
    import tempfile

    real = tempfile.mkdtemp

    def boom(*a, **k):
        if str(k.get("prefix", "")).startswith("xray-steps-recovery-"):
            raise PermissionError("no temp dir")
        return real(*a, **k)
    monkeypatch.setattr(tempfile, "mkdtemp", boom)
    res, saved = _fail_with_attachments(["a.png"])
    assert "Rebuilding the steps of P-1 failed" in res["error"] and "boom" in res["cause"]
    assert "recoveryDir" not in res
    assert saved == [{"filename": "a.png", "savedTo": None, "saveError": "no temp dir"}]
