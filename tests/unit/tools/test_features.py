from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import make_context
from xray_cloud.errors import ToolError
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "gherkin" / "regression"
YAML = "projects:\n  P: {}\n"
YAML_REQ = """
projects:
  P:
    fields:
      test:
        Severity: {id: customfield_1, type: option}
      precondition:
        Team: {id: customfield_2, type: option, required: true}
      testSet:
        Area: {id: customfield_3, type: option, required: true}
"""
READS = ("SearchTests", "GetPreconditions", "FindTestSets", "GetProjectId",
         "GetProjectSettings")


ANSWERS = {"createTestSet": True, "backgrounds": "preconditions", "tagsAsLabels": True,
           "aiLabel": ""}


def ans(**over):
    """Explicit answers to every import question (spec 6.2), overridable per test."""
    out = dict(ANSWERS)
    out.update(over)
    return out


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "my proj"
    (root / "features").mkdir(parents=True)
    shutil.copy(FIXTURES / "sample.feature", root / "features" / "login flow.feature")
    return root


class FakeXrayWorld:
    """Responses for a whole import: empty project unless told otherwise; creates count up."""

    def __init__(self, dup_summaries=(), existing=None, fail_on=None):
        self.n = 100
        self.dup_summaries = set(dup_summaries)
        self.existing = existing or {}
        self.fail_on = fail_on

    def _next(self):
        self.n += 1
        return str(self.n), "P-%d" % self.n

    def responses(self):
        def search(v):
            jql = v["jql"]
            for key, row in self.existing.items():
                if jql == "issue = %s" % key:
                    return {"getTests": {"total": 1, "results": [row]}}
            hits = [s for s in self.dup_summaries if '"%s"' % s in jql]
            return {"getTests": {"total": len(hits), "results": [
                {"issueId": "1", "jira": {"key": "P-1", "summary": s}} for s in hits]}}

        def create(op, wrap, node):
            def fn(v):
                if self.fail_on == op:
                    raise ToolError("Xray rejected %s" % op)
                iid, key = self._next()
                return {wrap: {node: {"issueId": iid, "jira": {"key": key}}, "warnings": []}}
            return fn

        return {
            "SearchTests": search,
            "GetPreconditions": {"getPreconditions": {"results": []}},
            "FindTestSets": {"getTestSets": {"results": []}},
            "CreateTest": create("CreateTest", "createTest", "test"),
            "CreatePrecondition": create("CreatePrecondition", "createPrecondition", "precondition"),
            "CreateTestSet": create("CreateTestSet", "createTestSet", "testSet"),
            "AddTestsToTestSet": self._maybe_fail("AddTestsToTestSet", {
                "addTestsToTestSet": {"addedTests": [], "warning": None}}),
            "UpdateGherkin": {"updateGherkinTestDefinition": {"issueId": "x"}},
            "AddTestsToPrecondition": {"addTestsToPrecondition": {"addedTests": [], "warning": None}},
            "UpdatePrecondition": {"updatePrecondition": {"issueId": "x"}},
        }

    def _maybe_fail(self, op, resp):
        def fn(v):
            if self.fail_on == op:
                raise ToolError("Xray rejected %s" % op)
            return resp
        return fn


def ctx_for(project, world=None, yaml=YAML):
    world = world or FakeXrayWorld()
    ctx = make_context(yaml, responses=world.responses())
    ctx.env["CLAUDE_PROJECT_DIR"] = str(project)
    return ctx


def writes(ctx):
    return [op for op, _ in ctx.xray.calls if op not in READS]


def test_preview_writes_nothing(project):
    ctx = ctx_for(project)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath="features/login flow.feature")
    assert not err, res
    assert res["preview"] is True and res["path"] == "features/login flow.feature"
    assert [a["kind"] for a in res["actions"]] == ["create_test", "create_test",
                                                   "create_precondition", "link",
                                                   "create_test_set"]
    assert res["actions"][2]["labels"] == ["features/login_flow.feature"]
    assert writes(ctx) == []


def test_apply_creates_tests_precondition_and_set_in_order(project):
    ctx = ctx_for(project)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath=str(project / "features" / "login flow.feature"), apply=True,
                    **ans())
    assert not err, res
    assert writes(ctx) == ["CreateTest", "CreateTest", "CreatePrecondition", "CreateTestSet",
                           "AddTestsToTestSet"]
    t1, t2 = ctx.xray.calls_of("CreateTest")
    assert t1["testType"] == {"name": "Cucumber"} and t1["jira"]["fields"]["summary"] == "Successful login"
    assert "| admin    | secret   |" not in t1["gherkin"] and "| admin | secret |" in t1["gherkin"]
    assert "Examples:" in t2["gherkin"]
    pre = ctx.xray.calls_of("CreatePrecondition")[0]
    assert pre["jira"]["fields"]["summary"] == "Background for: P-101, P-102"
    assert pre["jira"]["fields"]["labels"] == ["features/login_flow.feature"]
    assert pre["preconditionType"] == {"name": "Cucumber"} and pre["testIssueIds"] == ["101", "102"]
    assert pre["definition"] == "Given the app is running"
    assert ctx.xray.calls_of("CreateTestSet")[0]["jira"]["fields"]["summary"] == "Login"
    assert ctx.xray.calls_of("AddTestsToTestSet")[0] == {"issueId": "104",
                                                         "testIssueIds": ["101", "102"]}
    assert res["created"] == ["P-101", "P-102", "P-103", "P-104"]
    assert res["testSet"]["key"] == "P-104"


def test_create_test_set_false_and_requirement_links_on_new_tests(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("@REQ_Q-9 @team\nFeature: F\n  @ui\n  Scenario: A\n    Given a\n", encoding="utf-8")
    ctx = ctx_for(tmp_path, yaml=YAML_REQ)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, **ans(createTestSet=False), fields={"Severity": "High"},
                    rawFields={"labels": ["extra"], "customfield_7": 1})
    assert not err, res
    assert writes(ctx) == ["CreateTest"]
    jira = ctx.xray.calls_of("CreateTest")[0]["jira"]
    assert jira["fields"]["customfield_1"] == {"value": "High"}
    assert jira["fields"]["customfield_7"] == 1
    jira["fields"]["labels"].remove("extra")
    assert jira["fields"]["labels"] == ["team", "ui"]
    assert jira["update"] == {"issuelinks": [{"add": {"type": {"name": "Test"},
                                                      "outwardIssue": {"key": "Q-9"}}}]}


def test_key_tag_updates_existing_test(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  @TEST_P-5 @new\n  Scenario: A\n    Given changed\n", encoding="utf-8")
    world = FakeXrayWorld(existing={"P-5": {"issueId": "55", "testType": {"name": "Cucumber"},
                                            "jira": {"key": "P-5", "summary": "A", "labels": []}}})
    ctx = ctx_for(tmp_path, world)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert writes(ctx) == ["UpdateGherkin"]
    assert ctx.xray.calls_of("UpdateGherkin")[0] == {"issueId": "55", "gherkin": "Given changed"}
    assert res["updated"] == ["P-5"] and res["created"] == []
    assert res["notDone"] == [{"item": "P-5", "what": "labels new",
                               "reason": "no Jira token or Atlassian MCP",
                               "fix": "set the Jira token in plugin settings, or let the agent "
                                      "apply them via the Atlassian MCP"}]
    assert "warnings" not in res


def test_duplicates_skipped_unless_index_listed(project):
    world = FakeXrayWorld(dup_summaries=["Successful login"])
    ctx = ctx_for(project, world)
    res, _ = call(ctx, "create_tests_from_feature", projectKey="P",
                  featureFilePath="features/login flow.feature")
    assert res["possibleDuplicates"][0]["index"] == 0
    assert res["actions"][0]["duplicates"] == [{"key": "P-1", "summary": "Successful login"}]
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath="features/login flow.feature", apply=True, **ans())
    assert not err, res
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTest")] == ["Failed login"]
    assert res["skippedDuplicates"][0]["index"] == 0
    pre = ctx.xray.calls_of("CreatePrecondition")[0]
    assert pre["jira"]["fields"]["summary"] == "Background for: P-101"
    ctx = ctx_for(project, FakeXrayWorld(dup_summaries=["Successful login"]))
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", createDuplicates=[0],
                    **ans())
    assert not err and len(ctx.xray.calls_of("CreateTest")) == 2 and "skippedDuplicates" not in res
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", createDuplicates=[5],
                    **ans())
    assert err and "out of range" in res["error"]


def test_missing_required_field_refuses_whole_file_before_calls(project):
    ctx = ctx_for(project, yaml=YAML_REQ)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", testSetFields={"Area": "X"},
                    **ans())
    assert err and res["error"] == "Missing required fields"
    assert res["missingFields"] == [{"name": "Team", "id": "customfield_2"}]
    assert ctx.xray.calls == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", preconditionFields={"Team": "A"},
                    **ans())
    assert err and res["missingFields"] == [{"name": "Area", "id": "customfield_3"}]
    # createTestSet=false: testSet fields are not needed
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    **ans(createTestSet=False), preconditionFields={"Team": "A"},
                    featureFilePath="features/login flow.feature")
    assert not err, res
    assert ctx.xray.calls_of("CreatePrecondition")[0]["jira"]["fields"]["customfield_2"] == {"value": "A"}


def test_parse_error_creates_nothing_and_reports_line(tmp_path):
    f = tmp_path / "bad.feature"
    f.write_text("Feature: F\n  Scenario: A\n    Given a\n    | x |\n  oops not gherkin\n",
                 encoding="utf-8")
    ctx = ctx_for(tmp_path)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="bad.feature",
                    apply=True, **ans())
    assert err and res["line"] == 5 and "bad.feature" in res["error"]
    assert ctx.xray.calls == []


def test_missing_file_is_error_before_any_call(tmp_path):
    ctx = ctx_for(tmp_path)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="nope.feature")
    assert err and "not found" in res["error"]
    (tmp_path / "latin1.feature").write_bytes("Feature: caf\xe9\n".encode("latin-1"))
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath="latin1.feature")
    assert err and "UTF-8" in res["error"]
    assert ctx.xray.calls == []


def test_path_outside_project_dir_uses_basename(tmp_path, project):
    other = tmp_path / "elsewhere"
    other.mkdir()
    shutil.copy(FIXTURES / "sample.feature", other / "x y.feature")
    ctx = ctx_for(project)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath=str(other / "x y.feature"))
    assert not err and res["path"] == "x y.feature"
    assert res["actions"][2]["labels"] == ["x_y.feature"]


def test_without_claude_project_dir_cwd_is_the_project_dir(project, monkeypatch):
    ctx = ctx_for(project)
    del ctx.env["CLAUDE_PROJECT_DIR"]
    monkeypatch.chdir(project)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath="features/login flow.feature")
    assert not err, res
    assert res["path"] == "features/login flow.feature"


@pytest.mark.parametrize("fail_on,created,step", [
    ("CreatePrecondition", ["P-101", "P-102"], "creating Pre-Condition"),
    ("CreateTestSet", ["P-101", "P-102", "P-103"], "creating Test Set"),
    ("AddTestsToTestSet", ["P-101", "P-102", "P-103", "P-104"], "adding tests to Test Set P-104"),
])
def test_failure_after_some_creates_reports_created_keys(project, fail_on, created, step):
    ctx = ctx_for(project, FakeXrayWorld(fail_on=fail_on))
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert err
    assert res["created"] == created
    assert res["failedStep"].startswith(step)
    assert "Xray rejected" in res["cause"]


def test_permission_error_on_first_write_is_mapped(project):
    from xray_cloud.errors import ToolError
    ctx = ctx_for(project)

    def denied(_):
        raise ToolError("You do not have permission to create issues in this project")
    ctx.xray.responses["CreateTest"] = denied
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert err and res["reason"] == "permission_denied" and res["projectKey"] == "P"
    assert "create issues" in res["xrayMessage"] and "created" not in res


def test_failure_on_first_write_is_plain_error(project):
    ctx = ctx_for(project, FakeXrayWorld(fail_on="CreateTest"))
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert err and "created" not in res and "Xray rejected" in res["error"]


def test_reused_precondition_definition_updated_and_tests_linked(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  Background: Setup\n    Given new\n  Scenario: A\n    Given a\n",
                 encoding="utf-8")
    world = FakeXrayWorld()
    ctx = ctx_for(tmp_path, world)
    ctx.xray.responses["GetPreconditions"] = lambda v: {"getPreconditions": {"results": [
        {"issueId": "9", "preconditionType": {"name": "Cucumber"}, "definition": "Given old",
         "jira": {"key": "P-9", "summary": "Setup", "labels": ["a.feature"]}}]}
        if "labels" in v["jql"] else {"getPreconditions": {"results": []}}}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert writes(ctx) == ["CreateTest", "UpdatePrecondition", "AddTestsToPrecondition"]
    assert ctx.xray.calls_of("UpdatePrecondition")[0] == {"issueId": "9",
                                                         "data": {"definition": "Given new"}}
    assert ctx.xray.calls_of("AddTestsToPrecondition")[0] == {"issueId": "9", "testIssueIds": ["101"]}
    assert res["created"] == ["P-101"] and res["updated"] == ["P-9"]


def test_read_only_mode_allows_preview_refuses_apply(project):
    ctx = ctx_for(project)
    ctx.read_only = True
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath="features/login flow.feature")
    assert not err and res["preview"] is True and writes(ctx) == []
    ctx.xray.calls.clear()
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert err and "read-only" in res["error"]
    assert ctx.xray.calls == []


def test_apply_reuses_existing_test_set(project):
    ctx = ctx_for(project)
    ctx.xray.responses["FindTestSets"] = {"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "Login"}}]}}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert not err, res
    assert "CreateTestSet" not in writes(ctx)
    assert ctx.xray.calls_of("AddTestsToTestSet") == [{"issueId": "70",
                                                       "testIssueIds": ["101", "102"]}]
    assert res["testSet"] == {"key": "P-70", "summary": "Login", "created": False,
                              "addedTests": 2}
    assert res["created"] == ["P-101", "P-102", "P-103"]


def test_second_import_all_duplicates_writes_nothing(project):
    world = FakeXrayWorld(dup_summaries=["Successful login", "Failed login"])
    ctx = ctx_for(project, world)
    ctx.xray.responses["GetPreconditions"] = lambda v: {"getPreconditions": {"results": [
        {"issueId": "9", "preconditionType": {"name": "Cucumber"},
         "definition": "Given the app is running",
         "jira": {"key": "P-9", "summary": "Background for: P-1, P-2",
                  "labels": ["features/login_flow.feature"]}}]} if "labels" in v["jql"]
        else {"getPreconditions": {"results": []}}}
    ctx.xray.responses["FindTestSets"] = {"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "Login"}}]}}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert not err, res
    assert writes(ctx) == []
    assert res["created"] == [] and res["updated"] == []
    assert [d["index"] for d in res["skippedDuplicates"]] == [0, 1]
    assert res["skippedPreconditions"][0]["summary"] == "Background for: P-1, P-2"
    assert res["testSet"]["skipped"] is True


def test_reused_unnamed_precondition_keeps_its_summary(project):
    ctx = ctx_for(project)
    ctx.xray.responses["GetPreconditions"] = lambda v: {"getPreconditions": {"results": [
        {"issueId": "9", "preconditionType": {"name": "Cucumber"},
         "definition": "Given the app is running",
         "jira": {"key": "P-9", "summary": "Background for: P-1, P-2",
                  "labels": ["features/login_flow.feature"]}}]} if "labels" in v["jql"]
        else {"getPreconditions": {"results": []}}}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    **ans(createTestSet=False), featureFilePath="features/login flow.feature")
    assert not err, res
    assert writes(ctx) == ["CreateTest", "CreateTest", "AddTestsToPrecondition"]
    assert ctx.xray.calls_of("AddTestsToPrecondition")[0] == {"issueId": "9",
                                                              "testIssueIds": ["101", "102"]}
    assert res["preconditions"][0]["summary"] == "Background for: P-1, P-2"
    assert "warnings" not in res


class RecJira:
    def __init__(self, linked=()):
        self.edits, self.links, self.linked = [], [], list(linked)

    def edit_issue(self, key, fields):
        self.edits.append((key, fields))

    def linked_keys(self, key):
        return list(self.linked)

    def link_requirement(self, key, req):
        self.links.append((key, req))


def _existing_world():
    return FakeXrayWorld(existing={"P-5": {"issueId": "55", "testType": {"name": "Cucumber"},
                                           "jira": {"key": "P-5", "summary": "A", "labels": ["old"]}}})


def test_key_tag_update_with_jira_applies_labels_and_links(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  @TEST_P-5 @new @REQ_R-1 @REQ_R-2\n  Scenario: A\n    Given x\n",
                 encoding="utf-8")
    ctx = ctx_for(tmp_path, _existing_world())
    ctx.jira = RecJira(linked=["R-1"])
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert ctx.jira.edits == [("P-5", {"labels": ["old", "new"]})]
    assert ctx.jira.links == [("P-5", "R-2")]
    assert res["linkedRequirements"] == [{"test": "P-5", "requirements": ["R-2"]}]
    assert not any("not added" in w or "not created" in w for w in res.get("warnings", []))


def test_key_tag_update_without_jira_warns_about_links(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  @TEST_P-5 @REQ_R-1\n  Scenario: A\n    Given x\n", encoding="utf-8")
    ctx = ctx_for(tmp_path, _existing_world())
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert res["notDone"] == [{"item": "P-5", "what": "requirement links R-1",
                               "reason": "no Jira token or Atlassian MCP",
                               "fix": "set the Jira token in plugin settings, or let the agent "
                                      "apply them via the Atlassian MCP"}]


def test_partial_failure_reports_links_and_labels_already_applied(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  @TEST_P-5 @new @REQ_R-1 @REQ_R-2\n  Scenario: A\n    Given x\n",
                 encoding="utf-8")
    ctx = ctx_for(tmp_path, _existing_world())

    class Failing(RecJira):
        def link_requirement(self, key, req):
            if self.links:
                raise ToolError("Jira down")
            super().link_requirement(key, req)
    ctx.jira = Failing()
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, **ans(createTestSet=False))
    assert err and res["updated"] == ["P-5"]
    assert res["linkedRequirements"] == [{"test": "P-5", "requirements": ["R-1"]}]
    assert res["labelsApplied"] == [{"test": "P-5", "added": ["new"]}]


# -- import choices, tag rules, Test Sets from tags, notDone (spec 6.2-6.6, 8) ---------------

LOGIN = "features/login flow.feature"
NO_JIRA = {"reason": "no Jira token or Atlassian MCP",
           "fix": "set the Jira token in plugin settings, or let the agent apply them via the "
                  "Atlassian MCP"}
RULES_YAML = """
projects:
  P:
    fields:
      test:
        Component: {id: customfield_9, type: option}
      testSet:
        Area: {id: customfield_3, type: option, required: true, default: Core}
    featureImport:
      tagRules:
        - tag: suite
          label: "{value}"
        - tag: [area, areas]
          split: ","
          testSet: "{value}"
        - tag: comp
          field: {Component: "{value}"}
"""


def qids(res):
    return [q["id"] for q in res["questions"]]


def feature_file(tmp_path, text, name="a.feature"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return name


def preview(ctx, path, **args):
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path, **args)
    assert not err, res
    return res


def test_first_import_asks_apply_refuses_with_zero_calls_then_remembered(project):
    """Review Focus 1."""
    ctx = ctx_for(project)
    res = preview(ctx, LOGIN)
    assert qids(res) == ["createTestSet", "backgrounds", "aiLabel", "aiLabelScope"]
    q = {x["id"]: x for x in res["questions"]}
    assert q["createTestSet"]["context"]["testSetName"] == "Login"
    assert q["createTestSet"]["context"]["exists"] is False
    assert q["backgrounds"]["context"]["backgrounds"] == [
        {"name": "", "scope": "feature", "line": 3, "steps": 1}]
    assert q["aiLabel"]["context"]["wouldCreate"] == {"tests": 2, "preconditions": 1,
                                                       "testSets": 1}
    assert q["backgrounds"]["options"] == ["preconditions", "inline"]
    assert writes(ctx) == [] and ctx.preferences.get("global", "P") == {}
    ctx.xray.calls.clear()
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True)
    assert err and res["error"].startswith("Answer these first")
    assert qids(res) == ["createTestSet", "backgrounds", "aiLabel", "aiLabelScope"]
    assert ctx.xray.calls == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True, createTestSet=True, backgrounds="preconditions",
                    aiLabel="ai-made", aiLabelScope="tests")
    assert not err, res
    res = preview(ctx, LOGIN)
    assert res["questions"] == []
    assert res["choices"]["aiLabel"] == {"value": "ai-made", "source": "remembered"}
    assert res["choices"]["backgrounds"] == {"value": "preconditions", "source": "remembered"}
    ctx.xray.calls.clear()
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True)
    assert not err, res
    assert "ai-made" in ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"]


@pytest.mark.parametrize("text,args,expected", [
    # Feature without a name: no createTestSet question (and nothing to scope)
    ("Feature:\n  Scenario: A\n    Given a\n", {}, ["aiLabel"]),
    # no Background: no backgrounds question
    ("Feature: F\n  Scenario: A\n    Given a\n", {}, ["createTestSet", "aiLabel", "aiLabelScope"]),
    # a plain tag would become a label: tagsAsLabels asked
    ("Feature: F\n  @smoke\n  Scenario: A\n    Given a\n", {"createTestSet": False},
     ["tagsAsLabels", "aiLabel"]),
    # nothing new is created: no aiLabel question
    ("Feature: F\n  @TEST_P-5\n  Scenario: A\n    Given a\n", {"createTestSet": False}, []),
    # no label chosen: no aiLabelScope question
    ("Feature: F\n  Background:\n    Given b\n  Scenario: A\n    Given a\n",
     {"createTestSet": True, "backgrounds": "preconditions", "aiLabel": ""}, []),
    # a label, but no Pre-Conditions or Test Sets are created: no aiLabelScope question
    ("Feature: F\n  Background:\n    Given b\n  Scenario: A\n    Given a\n",
     {"createTestSet": False, "backgrounds": "inline", "aiLabel": "ai"}, []),
    # a label and a Pre-Condition: aiLabelScope asked
    ("Feature: F\n  Background:\n    Given b\n  Scenario: A\n    Given a\n",
     {"createTestSet": False, "backgrounds": "preconditions", "aiLabel": "ai"},
     ["aiLabelScope"]),
])
def test_not_applicable_questions_are_never_asked(tmp_path, text, args, expected):
    world = FakeXrayWorld(existing={"P-5": {"issueId": "55", "testType": {"name": "Cucumber"},
                                            "jira": {"key": "P-5", "summary": "A", "labels": []}}})
    ctx = ctx_for(tmp_path, world)
    path = feature_file(tmp_path, text)
    res = preview(ctx, path, **args)
    assert qids(res) == expected
    if not expected:  # nothing blocks apply
        res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                        apply=True, **args)
        assert not err, res


def test_rule_without_label_action_makes_tags_as_labels_not_applicable(tmp_path):
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    path = feature_file(tmp_path, "Feature: F\n  @area=ui @comp=x\n  Scenario: A\n    Given a\n")
    assert "tagsAsLabels" not in qids(preview(ctx, path))
    path = feature_file(tmp_path, "Feature: F\n  @suite=s\n  Scenario: A\n    Given a\n", "b.feature")
    assert "tagsAsLabels" in qids(preview(ctx, path))


def test_inline_backgrounds_create_no_preconditions_and_ignore_precond_tags(tmp_path):
    path = feature_file(tmp_path, "Feature: F\n  Background:\n    #@PRECOND_Q-7\n    Given bg\n"
                                  "  Scenario: A\n    Given a\n")
    ctx = ctx_for(tmp_path)
    res = preview(ctx, path, **ans(createTestSet=False, backgrounds="inline"))
    assert [a["kind"] for a in res["actions"]] == ["create_test"]
    assert any("#@PRECOND_Q-7" in w and "ignored" in w for w in res["warnings"])
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False, backgrounds="inline"))
    assert not err, res
    assert writes(ctx) == ["CreateTest"]
    assert ctx.xray.calls_of("CreateTest")[0]["gherkin"] == "Given bg\nGiven a"
    assert not ctx.xray.calls_of("GetPreconditions")
    assert any("#@PRECOND_Q-7" in w for w in res["warnings"])


def test_preview_shows_rule_outcomes_per_test_and_sets(tmp_path):
    shutil.copy(FIXTURES / "tags_keyvalue.feature", tmp_path / "t.feature")
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    ctx.xray.responses["FindTestSets"] = lambda v: {"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "ui"}}]} if '"ui"' in v["jql"]
        else {"getTestSets": {"results": []}}}
    res = preview(ctx, "t.feature", **ans())
    assert [(p["scenario"], p["labels"], p["testSets"], p["fields"]) for p in res["perTest"]] == [
        ("Checkout by card", ["regression", "team"], ["ui"], {}),
        ("Create account", ["regression", "team", "wip"], ["api", "users"],
         {"Component": "backend"}),
        ("Delete account", ["regression", "team"], ["api", "users"], {"Component": "backend"})]
    assert res["testSets"] == {
        "reuse": [{"name": "ui", "key": "P-70", "members": ["Checkout by card"]}],
        "create": [{"name": "Tagged checkout",
                    "members": ["Checkout by card", "Create account", "Delete account"]},
                   {"name": "api", "members": ["Create account", "Delete account"]},
                   {"name": "users", "members": ["Create account", "Delete account"]}]}
    assert res["notDone"] == [] and writes(ctx) == []
    assert res["choices"]["tagRules"] == {"count": 3, "source": "settings"}
    ctx.xray.calls.clear()
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="t.feature",
                    apply=True, **ans())
    assert not err, res
    t = ctx.xray.calls_of("CreateTest")
    assert t[1]["jira"]["fields"]["customfield_9"] == {"value": "backend"}
    assert t[1]["jira"]["fields"]["labels"] == ["regression", "team", "wip"]
    assert "customfield_9" not in t[0]["jira"]["fields"]
    created_sets = ctx.xray.calls_of("CreateTestSet")
    assert [c["jira"]["fields"]["summary"] for c in created_sets] == ["Tagged checkout", "api",
                                                                     "users"]
    assert all(c["jira"]["fields"]["customfield_3"] == {"value": "Core"} for c in created_sets)
    assert created_sets[0]["jira"]["fields"]["labels"] == ["regression", "team"]
    assert [m["name"] for m in res["testSetMemberships"]] == ["Tagged checkout", "ui", "api",
                                                             "users"]
    assert res["testSetMemberships"][1] == {"testSet": "P-70", "name": "ui", "tests": ["P-101"]}
    assert [s["name"] for s in res["testSetsCreated"]] == ["Tagged checkout", "api", "users"]
    assert res["testSet"]["summary"] == "Tagged checkout"


def test_required_test_set_field_for_tag_sets_validated_before_any_call(tmp_path):
    yaml = RULES_YAML.replace(", default: Core", "")
    ctx = ctx_for(tmp_path, yaml=yaml)
    path = feature_file(tmp_path, "Feature: F\n  @area=ui\n  Scenario: A\n    Given a\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert err and res["missingFields"] == [{"name": "Area", "id": "customfield_3"}]
    assert ctx.xray.calls == []


def test_ambiguous_tag_set_refused_and_feature_set_merged_with_tag_set(tmp_path):
    """Review Focus 4."""
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    path = feature_file(tmp_path, "Feature: ui\n  @area=ui\n  Scenario: A\n    Given a\n"
                                  "  @area=api\n  Scenario: B\n    Given b\n")
    ctx.xray.responses["FindTestSets"] = lambda v: {"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "api"}},
        {"issueId": "71", "jira": {"key": "P-71", "summary": "api"}}]} if '"api"' in v["jql"]
        else {"getTestSets": {"results": []}}}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans())
    assert err and "ambiguous" in res["error"] and "P-70" in res["error"] and "P-71" in res["error"]
    assert writes(ctx) == []
    ctx.xray.responses["FindTestSets"] = {"getTestSets": {"results": []}}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans())
    assert not err, res
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTestSet")] == [
        "ui", "api"]
    adds = ctx.xray.calls_of("AddTestsToTestSet")
    assert [len(a["testIssueIds"]) for a in adds] == [2, 1]


def _existing(key="P-5", issue_id="55", labels=(), component=None):
    return {key: {"issueId": issue_id, "testType": {"name": "Cucumber"},
                  "jira": {"key": key, "summary": "A", "labels": list(labels),
                           "customfield_9": component}}}


def test_existing_tests_join_reused_sets_without_jira(tmp_path):
    ctx = ctx_for(tmp_path, FakeXrayWorld(existing=_existing()), yaml=RULES_YAML)
    ctx.xray.responses["FindTestSets"] = {"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "ui"}}]}}
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @area=ui\n  Scenario: A\n    Given x\n"
                                  "  @area=ui\n  Scenario: B\n    Given y\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert res["testSets"] == {"reuse": [{"name": "ui", "key": "P-70", "members": ["P-5", "B"]}],
                               "create": []}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert ctx.xray.calls_of("AddTestsToTestSet") == [{"issueId": "70",
                                                       "testIssueIds": ["55", "101"]}]
    assert res["testSetMemberships"] == [{"testSet": "P-70", "name": "ui",
                                          "tests": ["P-5", "P-101"]}]
    assert res["notDone"] == [] and res["testSetsCreated"] == []


def test_rule_field_on_existing_test_never_overwrites(tmp_path):
    world = FakeXrayWorld(existing={**_existing(component={"value": "frontend"}),
                                    **_existing("P-6", "66")})
    ctx = ctx_for(tmp_path, world, yaml=RULES_YAML)
    ctx.jira = RecJira()
    path = feature_file(tmp_path, "@comp=backend\nFeature: F\n  @TEST_P-5\n  Scenario: A\n"
                                  "    Given x\n  @TEST_P-6\n  Scenario: B\n    Given y\n")
    expected = [{"item": "P-5", "what": "field Component = backend",
                 "reason": "already set to frontend",
                 "fix": "existing values are never overwritten; change it in Jira if wanted"}]
    res = preview(ctx, path, **ans(createTestSet=False))
    assert res["notDone"] == expected and ctx.jira.edits == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert res["notDone"] == expected
    assert ctx.jira.edits == [("P-6", {"customfield_9": {"value": "backend"}})]
    jira_reads = [v["jql"] for v in ctx.xray.calls_of("SearchTests")]
    assert "issue = P-5" in jira_reads


def test_not_done_items_for_existing_test_without_jira(tmp_path):
    ctx = ctx_for(tmp_path, FakeXrayWorld(existing=_existing(labels=["old"])), yaml=RULES_YAML)
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @new @REQ_R-1 @comp=backend\n"
                                  "  Scenario: A\n    Given x\n")
    expected = [dict(item="P-5", what="labels new", **NO_JIRA),
                dict(item="P-5", what="fields Component = backend", **NO_JIRA),
                dict(item="P-5", what="requirement links R-1", **NO_JIRA)]
    res = preview(ctx, path, **ans(createTestSet=False))
    assert res["notDone"] == expected
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert res["notDone"] == expected and "warnings" not in res
    assert writes(ctx) == ["UpdateGherkin"]


@pytest.mark.parametrize("scope,on_pre,on_set", [("tests", False, False), ("all", True, True)])
def test_ai_label_scope(project, scope, on_pre, on_set):
    world = FakeXrayWorld(existing=_existing(labels=["old"]))
    ctx = ctx_for(project, world)
    ctx.jira = RecJira()
    (project / "features" / "login flow.feature").write_text(
        (FIXTURES / "sample.feature").read_text(encoding="utf-8").replace(
            "  Scenario: Successful login", "  @TEST_P-5 @new\n  Scenario: Successful login"),
        encoding="utf-8")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True, **ans(aiLabel="ai-made", aiLabelScope=scope))
    assert not err, res
    assert "ai-made" in ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"]
    pre = ctx.xray.calls_of("CreatePrecondition")[0]["jira"]["fields"]["labels"]
    assert ("ai-made" in pre) is on_pre
    tset = ctx.xray.calls_of("CreateTestSet")[0]["jira"]["fields"].get("labels") or []
    assert ("ai-made" in tset) is on_set
    assert ctx.jira.edits == [("P-5", {"labels": ["old", "new"]})]  # never on existing tests


def test_settings_value_beats_remembered_one(tmp_path, project):
    yaml = "featureImport: {createTestSet: false}\nprojects:\n  P: {}\n"
    ctx = ctx_for(project, yaml=yaml)
    ctx.preferences.update("global", "P", {"createTestSet": True, "backgrounds": "inline"})
    res = preview(ctx, LOGIN, aiLabel="")
    assert res["choices"]["createTestSet"] == {"value": False, "source": "settings"}
    assert res["choices"]["backgrounds"] == {"value": "inline", "source": "remembered"}
    assert res["questions"] == []
    assert not any("test_set" in a["kind"] for a in res["actions"])


def test_preview_writes_and_remembers_nothing(tmp_path):
    shutil.copy(FIXTURES / "tags_keyvalue.feature", tmp_path / "t.feature")
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    ctx.jira = RecJira()
    res = preview(ctx, "t.feature", **ans(aiLabel="ai", aiLabelScope="all"))
    assert res["preview"] is True and res["questions"] == []
    assert writes(ctx) == [] and ctx.jira.edits == []
    assert ctx.preferences.get("global", "P") == {}


def test_apply_remembers_call_answers_and_remember_flag_overrides(project):
    ctx = ctx_for(project)
    args = ans(createTestSet=False, backgrounds="inline", aiLabel="a")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True, **args)
    assert not err, res
    assert ctx.preferences.get("global", "P") == {"createTestSet": False,
                                                  "backgrounds": "inline",
                                                  "tagsAsLabels": True, "aiLabel": "a"}
    call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN, apply=True,
         **ans(createTestSet=False, backgrounds="inline", aiLabel="b"))
    assert ctx.preferences.get("global", "P")["aiLabel"] == "a"
    call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN, apply=True,
         remember=True, **ans(createTestSet=False, backgrounds="inline", aiLabel="b"))
    assert ctx.preferences.get("global", "P")["aiLabel"] == "b"


def test_remembered_raw_tag_rules_are_parsed_for_the_project(project):
    ctx = ctx_for(project)
    ctx.preferences.update("global", "P", {"tagRules": [{"tag": "suite", "label": "s-{value}"}],
                                           "unmatchedTags": "skip"})
    (project / "features" / "login flow.feature").write_text(
        "Feature: F\n  @suite=x @other\n  Scenario: A\n    Given a\n", encoding="utf-8")
    res = preview(ctx, LOGIN, **ans())
    assert res["perTest"][0]["labels"] == ["s-x"]
    assert res["choices"]["tagRules"] == {"count": 1, "source": "remembered"}
    assert res["choices"]["unmatchedTags"] == {"value": "skip", "source": "remembered"}


def test_top_level_rule_field_unknown_for_project_refused_before_calls(tmp_path):
    yaml = """
featureImport:
  tagRules:
    - tag: comp
      field: {Component: "{value}"}
projects:
  P: {}
  Q:
    fields: {test: {Component: {id: customfield_9, type: option}}}
"""
    ctx = ctx_for(tmp_path, yaml=yaml)
    path = feature_file(tmp_path, "Feature: F\n  Scenario: A\n    Given a\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path)
    assert err and "Component" in res["error"] and ctx.xray.calls == []


def test_tag_problems_and_call_field_conflicts_refuse_before_calls(tmp_path):
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    path = feature_file(tmp_path, "@comp=a\nFeature: F\n  @comp=b\n  Scenario: A\n    Given a\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans())
    assert err and "conflicting" in res["error"] and ctx.xray.calls == []
    path = feature_file(tmp_path, "Feature: F\n  @comp=b\n  Scenario: A\n    Given a\n", "b.feature")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, fields={"Component": "c"}, **ans())
    assert err and "Component" in res["error"] and ctx.xray.calls == []


def test_preview_includes_tag_summary_and_stays_read_only(tmp_path):
    path = feature_file(tmp_path, "@suite=smoke\nFeature: F\n  @wip\n  Scenario: A\n    Given a\n")
    ctx = ctx_for(tmp_path)
    res = preview(ctx, path, **ans())
    s = res["tagSummary"]
    assert s["keyValue"]["suite"]["values"] == {"smoke": 1}
    assert s["plain"]["wip"]["count"] == 1 and s["unmatched"] == ["suite=smoke", "wip"]
    assert writes(ctx) == []


# ---- final-review fixes ------------------------------------------------------------------

def _comp_world(component, with_key=True):
    row = _existing(component=component)
    if not with_key:
        del row["P-5"]["jira"]["customfield_9"]
    return FakeXrayWorld(existing=row)


def test_rule_field_equal_to_existing_value_is_silent(tmp_path):
    """Final fix 1: an existing value equal to the rule value (after conversion) is neither a
    notDone item nor a write."""
    ctx = ctx_for(tmp_path, _comp_world({"value": "backend", "id": "10001"}), yaml=RULES_YAML)
    ctx.jira = RecJira()
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @comp=backend\n  Scenario: A\n"
                                  "    Given x\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert res["notDone"] == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert res["notDone"] == [] and ctx.jira.edits == [] and "fieldsApplied" not in res


def test_rule_field_unreadable_on_existing_test_is_not_done_never_written(tmp_path):
    """Final fix 2: a field id missing from Xray's `jira` response is not treated as empty."""
    ctx = ctx_for(tmp_path, _comp_world(None, with_key=False), yaml=RULES_YAML)
    ctx.jira = RecJira()
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @comp=backend\n  Scenario: A\n"
                                  "    Given x\n")
    expected = [{"item": "P-5", "what": "field Component = backend",
                 "reason": "current value could not be read; not changed",
                 "fix": "check the field id in the settings file; set it in Jira if wanted"}]
    res = preview(ctx, path, **ans(createTestSet=False))
    assert res["notDone"] == expected
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert res["notDone"] == expected and ctx.jira.edits == []


def test_existing_test_read_requests_rule_field_id(tmp_path):
    """Final fix 2: the existing-test read asks Xray for the custom field id."""
    ctx = ctx_for(tmp_path, _comp_world(None), yaml=RULES_YAML)
    queries = []
    inner = ctx.xray.graphql

    def spy(query, variables=None):
        queries.append((query, variables or {}))
        return inner(query, variables)
    ctx.xray.graphql = spy
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @comp=backend\n  Scenario: A\n"
                                  "    Given x\n")
    preview(ctx, path, **ans(createTestSet=False))
    reads = [q for q, v in queries if v.get("jql") == "issue = P-5"]
    assert reads and all('"customfield_9"' in q for q in reads)


def _fuzzy_sets(start, n=100):
    return [{"issueId": str(1000 + start + i),
             "jira": {"key": "P-%d" % (1000 + start + i), "summary": "ui extra %d" % (start + i)}}
            for i in range(n)]


def test_exact_test_set_match_on_a_later_page_is_reused(tmp_path):
    """Final fix 3: find_test_sets pages until a short page."""
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    exact = {"issueId": "70", "jira": {"key": "P-70", "summary": "ui"}}

    def sets(v):
        start = v.get("start") or 0
        if v["limit"] == 100 and start == 0:
            return {"getTestSets": {"results": _fuzzy_sets(0)}}
        if start == 100:
            return {"getTestSets": {"results": [exact]}}
        return {"getTestSets": {"results": []}}
    ctx.xray.responses["FindTestSets"] = sets
    path = feature_file(tmp_path, "Feature: F\n  @area=ui\n  Scenario: A\n    Given a\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert ctx.xray.calls_of("CreateTestSet") == []
    assert res["testSetMemberships"] == [{"testSet": "P-70", "name": "ui", "tests": ["P-101"]}]


def test_too_many_fuzzy_test_set_hits_refused_before_writes(tmp_path):
    """Final fix 3: when the page bound is hit, refuse instead of risking a duplicate set."""
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    ctx.xray.responses["FindTestSets"] = lambda v: {"getTestSets": {
        "results": _fuzzy_sets(v.get("start") or 0)}}
    path = feature_file(tmp_path, "Feature: F\n  @area=ui\n  Scenario: A\n    Given a\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert err and "'ui'" in res["error"] and "Test Sets" in res["error"]
    assert writes(ctx) == []


UNNAMED_RULES = RULES_YAML.replace("    featureImport:\n", "    featureImport:\n"
                                   "      createTestSet: true\n")


@pytest.mark.parametrize("source", ["settings", "remembered"])
def test_unnamed_feature_never_gets_a_feature_test_set(tmp_path, source):
    """Final fix 9 (spec 8): no Feature Test Set without a Feature name, whatever the settings
    or remembered createTestSet says; tag-derived sets are unaffected."""
    ctx = ctx_for(tmp_path, yaml=UNNAMED_RULES if source == "settings" else RULES_YAML)
    if source == "remembered":
        ctx.preferences.update("global", "P", {"createTestSet": True})
    path = feature_file(tmp_path, "Feature:\n  @area=ui\n  Scenario: A\n    Given a\n")
    answers = {k: v for k, v in ans().items() if k != "createTestSet"}
    res = preview(ctx, path, **answers)
    assert res["testSets"] == {"reuse": [], "create": [{"name": "ui", "members": ["A"]}]}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **answers)
    assert not err, res
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTestSet")] == ["ui"]
    assert "testSet" not in res or res["testSet"] is None or res["testSet"]["summary"] == "ui"
