from __future__ import annotations

import json

import pytest

from conftest import FakeXray
from xray_cloud import requiredfields
from xray_cloud.config import Credentials, JiraCredentials, SettingsStore
from xray_cloud.context import ServerContext
from xray_cloud.errors import NeedsAnswer, ToolError
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

SETTINGS = """
projects:
  PROJ:
    fields:
      test:
        Team: {id: customfield_1, type: option, default: Backend}
"""

META = [
    {"id": "summary", "name": "Summary", "required": True, "schemaType": "string",
     "allowedValues": []},
    {"id": "customfield_1", "name": "Team", "required": True, "schemaType": "option",
     "allowedValues": ["Backend", "Frontend"]},
    {"id": "customfield_2", "name": "Area", "required": True, "schemaType": "string",
     "allowedValues": []},
    {"id": "customfield_3", "name": "Severity", "required": True, "schemaType": "option",
     "allowedValues": ["High", "Low"]},
    {"id": "customfield_4", "name": "Notes", "required": False, "schemaType": "string",
     "allowedValues": []},
]
SEVERITY_ONLY = [META[0], META[3]]
COMPONENTS = [META[0], {"id": "components", "name": "Components", "required": True,
                        "schemaType": "array", "schemaItems": "component",
                        "allowedValues": ["Backend"]}]


class FakeJira:
    def __init__(self, meta):
        self.meta = meta
        self.calls = []

    def create_meta(self, pk, issue_type):
        self.calls.append((pk, issue_type))
        meta = self.meta.get(issue_type, []) if isinstance(self.meta, dict) else self.meta
        return [dict(f) for f in meta]


class FakeChannel:
    def __init__(self, answer=None, supports=True):
        self.answer = answer
        self.supports_elicitation = supports
        self.asked = []

    def elicit(self, message, schema):
        self.asked.append((message, schema))
        return self.answer


def make(tmp_path, settings="projects:\n  PROJ: {}\n", meta=None, channel=None, responses=None):
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path), "HOME": str(tmp_path / "home"),
           "XRAY_DATA_DIR": str(tmp_path / "data"),
           "XRAY_CLIENT_ID": "client-a", "XRAY_CLIENT_SECRET": "secret"}
    f = tmp_path / "settings.yml"
    f.write_text(settings)
    env["XRAY_SETTINGS_FILE"] = str(f)
    ctx = ServerContext(env, SettingsStore(env), Credentials("client-a", "secret", "global", "x"),
                        xray=FakeXray(responses or {}))
    ctx.jira = FakeJira(meta) if meta is not None else None
    ctx.channel = channel
    return ctx


def project(ctx, pk="PROJ"):
    from xray_cloud.config import ProjectSettings
    return ctx.settings_store.get().projects.get(pk) or ProjectSettings(pk, fields={})


def remembered(ctx, item_type="test"):
    return ctx.preferences.field_values("global", ctx.site, "PROJ", item_type)


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def created(key="PROJ-1"):
    return {"CreateTest": {"createTest": {"test": {"issueId": "1", "jira": {"key": key}},
                                          "warnings": []}}}


def test_order_call_settings_remembered_discovered(tmp_path):
    ctx = make(tmp_path, SETTINGS, meta=META)
    ctx.preferences.remember_fields("global", ctx.site, "PROJ", "test",
                                    {"customfield_1": {"value": "Old"}, "customfield_2": "Core"})
    fields, applied = requiredfields.resolve(ctx, project(ctx), "test",
                                             {"customfield_3": "High"}, None, False)
    assert fields == {"customfield_1": {"value": "Backend"},  # settings default beats remembered
                      "customfield_2": "Core",                # remembered
                      "customfield_3": {"value": "High"}}     # call, by discovered field id
    assert applied == {"customfield_2": "Core"}
    # the call beats settings and remembered; discovered names work too
    fields, applied = requiredfields.resolve(ctx, project(ctx), "test",
                                             {"Team": "Frontend", "Severity": "Low"},
                                             {"customfield_2": "Raw"}, False)
    assert fields == {"customfield_1": {"value": "Frontend"}, "customfield_2": "Raw",
                      "customfield_3": {"value": "Low"}}
    assert applied == {}


def test_missing_required_with_token_asks_via_elicitation_and_remembers(tmp_path):
    ch = FakeChannel({"customfield_3": "High"})
    ctx = make(tmp_path, meta=SEVERITY_ONLY, channel=ch, responses=created())
    res, err = call(ctx, "create_test", projectKey="PROJ", summary="S", aiLabel="",
                    allowDuplicates=True)
    assert not err and res["created"] is True, res
    message, schema = ch.asked[0]
    assert schema["type"] == "object" and schema["required"] == ["customfield_3"]
    prop = schema["properties"]["customfield_3"]
    assert prop["type"] == "string" and prop["enum"] == ["High", "Low"]
    assert prop["title"] == "Severity"
    sent = ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]
    assert sent["customfield_3"] == {"value": "High"}
    assert remembered(ctx) == {"customfield_3": {"value": "High"}}
    assert "appliedDefaults" not in res
    # next time: no question, the remembered value is used and reported
    res, err = call(ctx, "create_test", projectKey="PROJ", summary="T", aiLabel="",
                    allowDuplicates=True)
    assert not err and len(ch.asked) == 1
    assert res["appliedDefaults"] == {"test": {"customfield_3": {"value": "High"}}}


@pytest.mark.parametrize("answer", [None, {}, {"customfield_3": ""}])
def test_elicitation_decline_writes_and_remembers_nothing(tmp_path, answer):
    ch = FakeChannel(answer)
    ctx = make(tmp_path, meta=SEVERITY_ONLY, channel=ch, responses=created())
    res, err = call(ctx, "create_test", projectKey="PROJ", summary="S", aiLabel="",
                    allowDuplicates=True)
    assert err and "nothing was written" in res["error"]
    assert len(ch.asked) == 1 and ctx.xray.calls == [] and remembered(ctx) == {}


def test_missing_required_without_elicitation_needs_answer_nothing_written(tmp_path):
    for i, channel in enumerate((None, FakeChannel(supports=False))):
        (tmp_path / str(i)).mkdir()
        ctx = make(tmp_path / str(i), meta=SEVERITY_ONLY, channel=channel, responses=created())
        res, err = call(ctx, "create_test", projectKey="PROJ", summary="S", aiLabel="",
                        allowDuplicates=True)
        assert not err and res["status"] == "needs_answer"
        q = res["questions"][0]
        assert q["id"] == "customfield_3" and q["name"] == "Severity"
        assert q["type"] == "option" and q["choices"] == ["High", "Low"]
        assert "fields" in res["message"]
        assert ctx.xray.calls == [] and remembered(ctx) == {}
        # the follow-up passes `fields`: created, and the answer is remembered
        res, err = call(ctx, "create_test", projectKey="PROJ", summary="S", aiLabel="",
                        allowDuplicates=True, fields={"customfield_3": "Low"})
        assert not err and res["created"] is True
        assert remembered(ctx) == {"customfield_3": {"value": "Low"}}


def test_unexpressible_field_goes_to_needs_answer_even_with_elicitation(tmp_path):
    ch = FakeChannel({"components": "Backend"})
    ctx = make(tmp_path, meta=COMPONENTS, channel=ch)
    with pytest.raises(NeedsAnswer) as ei:
        requiredfields.resolve(ctx, project(ctx), "test", None, None, False)
    assert ch.asked == [] and ei.value.questions[0]["id"] == "components"
    fields, _ = requiredfields.resolve(ctx, project(ctx), "test",
                                       {"Components": ["Backend"]}, None, False)
    assert fields == {"components": [{"name": "Backend"}]}


def test_many_allowed_values_listed_up_to_fifty(tmp_path):
    meta = [{"id": "customfield_5", "name": "Pick", "required": True, "schemaType": "option",
             "allowedValues": ["v%d" % i for i in range(60)]}]
    ctx = make(tmp_path, meta=meta)
    with pytest.raises(NeedsAnswer) as ei:
        requiredfields.resolve(ctx, project(ctx), "test", None, None, False)
    q = ei.value.questions[0]
    assert len(q["choices"]) == 50 and q["moreChoices"] == 10
    ch = FakeChannel({"customfield_5": "v55"})
    (tmp_path / "b").mkdir()
    ctx = make(tmp_path / "b", meta=meta, channel=ch)
    fields, _ = requiredfields.resolve(ctx, project(ctx), "test", None, None, False)
    assert "enum" not in ch.asked[0][1]["properties"]["customfield_5"]
    assert fields == {"customfield_5": {"value": "v55"}}


def test_remember_replaces_only_when_asked(tmp_path):
    ctx = make(tmp_path, meta=SEVERITY_ONLY)
    ctx.preferences.remember_fields("global", ctx.site, "PROJ", "test",
                                    {"customfield_3": {"value": "High"}})
    requiredfields.resolve(ctx, project(ctx), "test", {"Severity": "Low"}, None, False)
    requiredfields.commit(ctx)
    assert remembered(ctx) == {"customfield_3": {"value": "High"}}
    requiredfields.resolve(ctx, project(ctx), "test", {"Severity": "Low"}, None, True)
    requiredfields.commit(ctx)
    assert remembered(ctx) == {"customfield_3": {"value": "Low"}}


def test_applied_defaults_reported(tmp_path):
    ctx = make(tmp_path, meta=SEVERITY_ONLY, responses=created())
    ctx.preferences.remember_fields("global", ctx.site, "PROJ", "test",
                                    {"customfield_3": {"value": "High"}})
    res, err = call(ctx, "create_test", projectKey="PROJ", summary="S", aiLabel="",
                    allowDuplicates=True)
    assert not err and res["appliedDefaults"] == {"test": {"customfield_3": {"value": "High"}}}
    # a call value wins and nothing is reported
    res, err = call(ctx, "create_test", projectKey="PROJ", summary="S", aiLabel="",
                    allowDuplicates=True, fields={"Severity": "Low"})
    assert not err and "appliedDefaults" not in res


def test_createmeta_cached_per_run(tmp_path):
    ctx = make(tmp_path, meta=SEVERITY_ONLY)
    for _ in range(3):
        requiredfields.resolve(ctx, project(ctx), "test", {"Severity": "Low"}, None, False)
    assert ctx.jira.calls == [("PROJ", "Test")]
    with pytest.raises(NeedsAnswer):
        requiredfields.resolve(ctx, project(ctx), "testExecution", None, None, False)
    assert ctx.jira.calls == [("PROJ", "Test"), ("PROJ", "Test Execution")]


def test_no_token_no_settings_passes_through_unchanged(tmp_path):
    ctx = make(tmp_path)
    assert ctx.jira is None
    fields, applied = requiredfields.resolve(ctx, project(ctx), "test", None,
                                             {"customfield_9": "x"}, False)
    assert fields == {"customfield_9": "x"} and applied == {}
    with pytest.raises(ToolError) as ei:
        requiredfields.resolve(ctx, project(ctx), "test", {"Nope": 1}, None, False)
    assert ei.value.data["unknownFields"] == ["Nope"]


def test_createmeta_failure_falls_back_to_pass_through(tmp_path):
    ctx = make(tmp_path, meta=SEVERITY_ONLY)

    def boom(pk, issue_type):
        raise ToolError("Jira is down")
    ctx.jira.create_meta = boom
    fields, applied = requiredfields.resolve(ctx, project(ctx), "test", None, None, False)
    assert fields == {} and applied == {}


def test_settings_required_field_missing_asks(tmp_path):
    settings = ("projects:\n  PROJ:\n    fields:\n      test:\n"
                "        Team: {id: customfield_1, type: option, required: true}\n")
    ctx = make(tmp_path, settings)
    with pytest.raises(NeedsAnswer) as ei:
        requiredfields.resolve(ctx, project(ctx), "test", None, None, False)
    assert ei.value.data["missingFields"] == [{"name": "Team", "id": "customfield_1"}]
    fields, _ = requiredfields.resolve(ctx, project(ctx), "test", {"customfield_1": "Backend"},
                                       None, False)
    assert fields == {"customfield_1": {"value": "Backend"}}


def test_container_new_tests_asked_with_test_fields_param(tmp_path):
    ctx = make(tmp_path, meta=SEVERITY_ONLY, responses=dict(created(), **{
        "CreateTestSet": {"createTestSet": {"testSet": {"issueId": "900",
                                                        "jira": {"key": "PROJ-10"}},
                                            "warnings": []}},
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "1",
                                                      "jira": {"key": "PROJ-1"}}]}},
        "AddTestsToTestSet": {"addTestsToTestSet": {"addedTests": ["1"], "warning": None}}}))
    args = dict(projectKey="PROJ", summary="S", aiLabel="", newTests=[{"summary": "A"}],
                createDuplicates=[0], fields={"Severity": "Low"})
    res, err = call(ctx, "create_test_set", **args)
    assert not err and res["status"] == "needs_answer"
    assert res["questions"][0]["param"] == "testFields" and ctx.xray.calls == []
    res, err = call(ctx, "create_test_set", testFields={"Severity": "High"}, **args)
    assert not err and res["createdTests"] == ["PROJ-1"], res
    assert ctx.xray.calls_of("CreateTestSet")[0]["jira"]["fields"]["customfield_3"] == \
        {"value": "Low"}
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["customfield_3"] == \
        {"value": "High"}
    assert remembered(ctx, "testSet") == {"customfield_3": {"value": "Low"}}
    assert remembered(ctx, "test") == {"customfield_3": {"value": "High"}}
    del args["fields"]
    res, err = call(ctx, "create_test_set", **args)
    assert not err and res["appliedDefaults"] == {"testSet": {"customfield_3": {"value": "Low"}},
                                                  "test": {"customfield_3": {"value": "High"}}}


def test_feature_import_without_background_never_asks_precondition_fields(tmp_path):
    ctx = make(tmp_path, meta=SEVERITY_ONLY, channel=FakeChannel(supports=False))
    ctx.xray.rest_responses[("POST", "import/feature")] = {"updatedOrCreatedTests": []}
    (tmp_path / "a.feature").write_text("Feature: F\n  Scenario: S\n    Given x\n")
    res, err = call(ctx, "import_feature_file", projectKey="PROJ", filePath="a.feature",
                    fields={"Severity": "High"})
    assert not err and "status" not in res, res
    files = ctx.xray.rest_calls[0][2]["multipart"]["files"]
    assert "precondInfo" not in files
    assert json.loads(files["testInfo"][1])["fields"] == {"customfield_3": {"value": "High"}}
    (tmp_path / "b.feature").write_text("Feature: F\n  Background:\n    Given y\n"
                                        "  Scenario: S\n    Given x\n")
    res, err = call(ctx, "import_feature_file", projectKey="PROJ", filePath="b.feature",
                    fields={"Severity": "High"})
    assert not err and res["status"] == "needs_answer"
    assert res["questions"][0]["param"] == "preconditionFields"


def test_results_new_execution_with_discovered_fields_goes_multipart(tmp_path):
    ctx = make(tmp_path, meta=SEVERITY_ONLY)
    ctx.xray.rest_responses[("POST", "import/execution/junit/multipart")] = {
        "id": "100", "key": "PROJ-9"}
    ctx.xray.responses["ResultRunSummary"] = {"getTestExecution": {"testRuns": {
        "total": 0, "results": []}}}
    res, err = call(ctx, "import_execution_results", projectKey="PROJ", format="junit",
                    content="<x/>")
    assert not err and res["status"] == "needs_answer" and ctx.xray.rest_calls == []
    res, err = call(ctx, "import_execution_results", projectKey="PROJ", format="junit",
                    content="<x/>", fields={"Severity": "High"})
    assert not err, res
    info = json.loads(ctx.xray.rest_calls[0][2]["multipart"]["files"]["info"][1])
    assert info["fields"]["customfield_3"] == {"value": "High"}
    res, err = call(ctx, "import_execution_results", projectKey="PROJ", format="junit",
                    content="<x/>")
    assert not err and res["appliedDefaults"] == {
        "testExecution": {"customfield_3": {"value": "High"}}}


def test_required_field_with_jira_default_or_set_by_the_tool_is_not_asked(tmp_path):
    meta = [dict(META[3], hasDefaultValue=True),
            {"id": "description", "name": "Description", "required": True,
             "schemaType": "string", "allowedValues": []}]
    ctx = make(tmp_path, meta=meta)
    fields, _ = requiredfields.resolve(ctx, project(ctx), "test", None, None, False,
                                       provided=["description"])
    assert fields == {}


BG_FEATURE = "Feature: F\n  Background:\n    Given y\n  Scenario: S\n    Given x\n"


def test_multi_item_unformable_field_one_needs_answer_nothing_elicited(tmp_path):
    ch = FakeChannel({"x": "y"})
    ctx = make(tmp_path, meta={"Test": SEVERITY_ONLY, "Precondition": COMPONENTS}, channel=ch)
    (tmp_path / "b.feature").write_text(BG_FEATURE)
    res, err = call(ctx, "import_feature_file", projectKey="PROJ", filePath="b.feature")
    assert not err and res["status"] == "needs_answer" and ch.asked == []
    assert [(q["id"], q["param"]) for q in res["questions"]] == [
        ("customfield_3", "fields"), ("components", "preconditionFields")]
    assert ctx.xray.rest_calls == [] and remembered(ctx) == {}


def test_multi_item_elicitation_one_form_then_applied_defaults(tmp_path):
    ch = FakeChannel({"test.customfield_3": "High", "precondition.customfield_3": "Low"})
    ctx = make(tmp_path, meta=SEVERITY_ONLY, channel=ch)
    ctx.xray.rest_responses[("POST", "import/feature")] = {"updatedOrCreatedTests": []}
    (tmp_path / "b.feature").write_text(BG_FEATURE)
    res, err = call(ctx, "import_feature_file", projectKey="PROJ", filePath="b.feature")
    assert not err and "status" not in res, res
    assert len(ch.asked) == 1
    assert ch.asked[0][1]["required"] == ["test.customfield_3", "precondition.customfield_3"]
    files = ctx.xray.rest_calls[0][2]["multipart"]["files"]
    assert json.loads(files["testInfo"][1])["fields"] == {"customfield_3": {"value": "High"}}
    assert json.loads(files["precondInfo"][1])["fields"] == {"customfield_3": {"value": "Low"}}
    res, err = call(ctx, "import_feature_file", projectKey="PROJ", filePath="b.feature")
    assert not err and len(ch.asked) == 1
    assert res["appliedDefaults"] == {"test": {"customfield_3": {"value": "High"}},
                                      "precondition": {"customfield_3": {"value": "Low"}}}


def test_xray_owned_fields_are_never_asked(tmp_path):
    meta = [dict(META[2], schemaCustom="com.xpandit.plugins.xray:test-type-custom-field")]
    ctx = make(tmp_path, meta=meta)
    assert requiredfields.resolve(ctx, project(ctx), "test", None, None, False) == ({}, {})


def test_settings_required_field_not_on_create_screen_uses_remembered(tmp_path):
    settings = ("projects:\n  PROJ:\n    fields:\n      test:\n"
                "        Team: {id: customfield_1, type: option, required: true}\n")
    ctx = make(tmp_path, settings, meta=SEVERITY_ONLY[:1], channel=FakeChannel(
        {"customfield_1": "Backend"}))
    requiredfields.resolve(ctx, project(ctx), "test", None, None, False)
    requiredfields.commit(ctx)
    assert remembered(ctx) == {"customfield_1": {"value": "Backend"}}
    fields, applied = requiredfields.resolve(ctx, project(ctx), "test", None, None, False)
    assert fields == {"customfield_1": {"value": "Backend"}} and len(ctx.channel.asked) == 1
