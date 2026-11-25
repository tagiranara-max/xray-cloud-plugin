from __future__ import annotations

import json
import re
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
         "GetProjectSettings", "FindCoverableIssues")


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

    def __init__(self, dup_summaries=(), existing=None, fail_on=None, coverable=()):
        self.coverable = list(coverable)  # requirement keys Xray knows (getCoverableIssues)
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
            m = re.fullmatch(r"issue = (\S+)|issue in \((.*)\)", jql)
            if m:
                keys = [m.group(1)] if m.group(1) else m.group(2).split(", ")
                rows = [self.existing[k] for k in keys if k in self.existing]
                return {"getTests": {"total": len(rows), "results": rows}}
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

        def coverable(v):
            m = re.fullmatch(r"issue = (\S+)|issue in \((.*)\)", v["jql"])
            keys = [m.group(1)] if m.group(1) else m.group(2).split(", ")
            return {"getCoverableIssues": {"results": [
                {"issueId": "9", "jira": {"key": k, "issuetype": {"name": "Story"}}}
                for k in keys if k in self.coverable]}}

        return {
            "FindCoverableIssues": coverable,
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
    ctx = ctx_for(tmp_path, FakeXrayWorld(coverable=["Q-9"]), yaml=YAML_REQ)
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
                    apply=True, existingTests="update", **ans(createTestSet=False))
    assert not err, res
    assert writes(ctx) == ["UpdateGherkin"]
    assert ctx.xray.calls_of("UpdateGherkin")[0] == {"issueId": "55", "gherkin": "Given changed"}
    assert res["updated"] == ["P-5"] and res["created"] == []
    assert res["notDone"] == [{"item": "P-5", "what": "labels new",
                               "reason": "no Jira token or Atlassian MCP",
                               "fix": NO_JIRA_FIX}]
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
    assert not err and res["status"] == "needs_answer"
    assert res["missingFields"] == [{"name": "Team", "id": "customfield_2"}]
    assert res["questions"][0]["param"] == "preconditionFields"
    assert writes(ctx) == []  # only reads (the duplicate search) before the question
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", preconditionFields={"Team": "A"},
                    **ans())
    assert res["status"] == "needs_answer"
    assert res["missingFields"] == [{"name": "Area", "id": "customfield_3"}]
    assert res["questions"][0]["param"] == "testSetFields"
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


@pytest.mark.parametrize("op,param", [("CreatePrecondition", "preconditionFields"),
                                      ("CreateTestSet", "testSetFields")])
def test_later_item_type_refused_for_required_fields_is_accurate_partial_failure(
        project, op, param):
    ctx = ctx_for(project)
    jira_error = "Create issue failed! - components: Components is required."

    def refuse(_):
        raise ToolError(jira_error)
    ctx.xray.responses[op] = refuse
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert err and res["created"][:2] == ["P-101", "P-102"], res
    assert "Nothing was created" not in res["cause"] and "`%s`" % param in res["cause"]
    assert res["missingFields"] == [{"name": "Components", "id": "components"}]
    assert res["jiraError"] == jira_error


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

    def create_meta(self, pk, issue_type):
        return []


def test_similar_summaries_are_listed_but_never_skipped(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  Scenario: Sign in\n    Given a\n"
                 "  Scenario: Sign out\n    Given b\n", encoding="utf-8")
    world = FakeXrayWorld()
    base = world.responses()

    def search(v):
        if "Sign out" in v["jql"]:
            return {"getTests": {"total": 1, "results": [
                {"issueId": "2", "jira": {"key": "P-2", "summary": " sign  OUT "}}]}}
        return {"getTests": {"total": 1, "results": [
            {"issueId": "1", "jira": {"key": "P-1", "summary": "Devices I signed in from"}}]}}
    base["SearchTests"] = search
    ctx = make_context(YAML, responses=base)
    ctx.env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    **ans(createTestSet=False))
    assert not err, res
    a0, a1 = res["actions"][0], res["actions"][1]
    assert "duplicates" not in a0
    assert a0["similar"] == [{"key": "P-1", "summary": "Devices I signed in from"}]
    assert a1["duplicates"] == [{"key": "P-2", "summary": " sign  OUT "}]
    assert [d["index"] for d in res["possibleDuplicates"]] == [1]
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTest")] == ["Sign in"]
    assert [d["index"] for d in res["skippedDuplicates"]] == [1]


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
                    apply=True, existingTests="update", **ans(createTestSet=False))
    assert not err, res
    assert ctx.jira.edits == [("P-5", {"labels": ["old", "new"]})]
    assert ctx.jira.links == [("P-5", "R-2")]
    assert res["linkedRequirements"] == [{"test": "P-5", "requirements": ["R-2"]}]
    assert not any("not added" in w or "not created" in w for w in res.get("warnings", []))


def test_key_tag_update_without_jira_warns_about_links(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  @TEST_P-5 @REQ_R-1\n  Scenario: A\n    Given x\n", encoding="utf-8")
    world = _existing_world()
    world.coverable = ["R-1"]
    ctx = ctx_for(tmp_path, world)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, existingTests="update", **ans(createTestSet=False))
    assert not err, res
    assert res["notDone"] == [{"item": "P-5", "what": "requirement links R-1",
                               "reason": "no Jira token or Atlassian MCP",
                               "fix": NO_JIRA_FIX}]


def test_partial_failure_reports_links_and_labels_already_applied(tmp_path):
    f = tmp_path / "a.feature"
    f.write_text("Feature: F\n  @TEST_P-5 @new @REQ_R-1 @REQ_R-2\n  Scenario: A\n    Given x\n",
                 encoding="utf-8")
    world = _existing_world()
    world.coverable = ["R-1", "R-2"]  # requirements Xray knows: a failure is real
    ctx = ctx_for(tmp_path, world)

    class Failing(RecJira):
        def link_requirement(self, key, req):
            if self.links:
                raise ToolError("Jira down")
            super().link_requirement(key, req)
    ctx.jira = Failing()
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath="a.feature",
                    apply=True, existingTests="update", **ans(createTestSet=False))
    assert err and res["updated"] == ["P-5"]
    assert res["linkedRequirements"] == [{"test": "P-5", "requirements": ["R-1"]}]
    assert res["labelsApplied"] == [{"test": "P-5", "added": ["new"]}]


# -- import choices, tag rules, Test Sets from tags, notDone (spec 6.2-6.6, 8) ---------------

LOGIN = "features/login flow.feature"
from xray_cloud.notdone import NO_JIRA_FIX  # noqa: E402
NO_JIRA = {"reason": "no Jira token or Atlassian MCP",
           "fix": NO_JIRA_FIX}
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
                                            "gherkin": "Given a",  # nothing would change
                                            "jira": {"key": "P-5", "summary": "A", "labels": []}}})
    ctx = ctx_for(tmp_path, world)
    path = feature_file(tmp_path, text)
    res = preview(ctx, path, **args)
    assert qids(res) == expected
    if not expected:  # nothing blocks apply
        res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                        apply=True, **args)
        assert not err, res


def test_tags_as_labels_is_asked_for_plain_tags_only(tmp_path):
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    path = feature_file(tmp_path, "Feature: F\n  @area=ui @comp=x\n  Scenario: A\n    Given a\n")
    assert "tagsAsLabels" not in qids(preview(ctx, path))
    # a rule's label action is the rule's own answer, not a tagsAsLabels question
    path = feature_file(tmp_path, "Feature: F\n  @suite=s\n  Scenario: A\n    Given a\n", "b.feature")
    assert "tagsAsLabels" not in qids(preview(ctx, path))
    path = feature_file(tmp_path, "Feature: F\n  @suite=s @smoke\n  Scenario: A\n    Given a\n",
                        "c.feature")
    res = preview(ctx, path)
    assert "tagsAsLabels" in qids(res)
    assert res["questions"][qids(res).index("tagsAsLabels")]["context"]["tags"] == ["smoke"]


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
    assert not err and res["status"] == "needs_answer"
    assert res["missingFields"] == [{"name": "Area", "id": "customfield_3"}]
    assert writes(ctx) == []


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
    res = preview(ctx, path, existingTests="update", **ans(createTestSet=False))
    assert res["testSets"] == {"reuse": [{"name": "ui", "key": "P-70", "members": ["P-5", "B"]}],
                               "create": []}
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, existingTests="update", **ans(createTestSet=False))
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
    res = preview(ctx, path, existingTests="update", **ans(createTestSet=False))
    assert res["notDone"] == expected and ctx.jira.edits == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, existingTests="update", **ans(createTestSet=False))
    assert not err, res
    assert res["notDone"] == expected
    assert ctx.jira.edits == [("P-6", {"customfield_9": {"value": "backend"}})]
    jira_reads = [v["jql"] for v in ctx.xray.calls_of("SearchTests")]
    assert "issue = P-5" in jira_reads


def test_not_done_items_for_existing_test_without_jira(tmp_path):
    ctx = ctx_for(tmp_path, FakeXrayWorld(existing=_existing(labels=["old"]), coverable=["R-1"]), yaml=RULES_YAML)
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @new @REQ_R-1 @comp=backend\n"
                                  "  Scenario: A\n    Given x\n")
    expected = [dict(item="P-5", what="labels new", **NO_JIRA),
                dict(item="P-5", what="fields Component = backend", **NO_JIRA),
                dict(item="P-5", what="requirement links R-1", **NO_JIRA)]
    res = preview(ctx, path, existingTests="update", **ans(createTestSet=False))
    assert res["notDone"] == expected
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, existingTests="update", **ans(createTestSet=False))
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
                    apply=True, existingTests="update", **ans(aiLabel="ai-made", aiLabelScope=scope))
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


def test_tag_problems_refuse_before_calls_and_the_calls_field_beats_a_rule(tmp_path):
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    path = feature_file(tmp_path, "@comp=a\nFeature: F\n  @comp=b\n  Scenario: A\n    Given a\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans())
    assert err and "conflicting" in res["error"] and ctx.xray.calls == []
    # a value the call passes for the rule's field wins; never a conflict refusal
    path = feature_file(tmp_path, "Feature: F\n  @comp=b\n  Scenario: A\n    Given a\n", "b.feature")
    for given in ({"fields": {"Component": "c"}},
                  {"rawFields": {"customfield_9": {"value": "c"}}}):
        ctx = ctx_for(tmp_path, yaml=RULES_YAML)
        res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                        apply=True, **ans(createTestSet=False), **given)
        assert not err, res
        sent = ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]
        assert sent["customfield_9"] == {"value": "c"}


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
    res = preview(ctx, path, existingTests="update", **ans(createTestSet=False))
    assert res["notDone"] == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, existingTests="update", **ans(createTestSet=False))
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
    res = preview(ctx, path, existingTests="update", **ans(createTestSet=False))
    assert res["notDone"] == expected
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, existingTests="update", **ans(createTestSet=False))
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
    assert reads and '"customfield_9"' in reads[-1]  # the first read is the key lookup


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


def test_null_optional_answers_behave_like_omitted(project):
    base = dict(projectKey="P", featureFilePath="features/login flow.feature")
    nulls = dict(base, aiLabel=None, aiLabelScope=None, createTestSet=None,
                 tagsAsLabels=None, backgrounds=None)
    a, err_a = call(ctx_for(project), "create_tests_from_feature", **base)
    b, err_b = call(ctx_for(project), "create_tests_from_feature", **nulls)
    assert (a, err_a) == (b, err_b)


def _unlisted_ctx(project):
    ctx = ctx_for(project, yaml="projects:\n  Q: {}\n")  # P is not listed
    ctx.env.update(XRAY_CLIENT_ID="client-a", XRAY_CLIENT_SECRET="secret")
    return ctx


def test_feature_import_takes_allow_project():
    schema = next(t["inputSchema"] for t in REGISTRY.list()
                  if t["name"] == "create_tests_from_feature")
    assert schema["properties"]["allowProject"]["enum"] == ["all", "project"]


def test_apply_on_unlisted_project_asks_then_allow_project_all_applies(project):
    ctx = _unlisted_ctx(project)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    featureFilePath="features/login flow.feature", **ans())
    assert not err and res["status"] == "needs_answer", res
    assert res["questions"][0]["id"] == "allowProject" and writes(ctx) == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", apply=True,
                    allowProject="all", featureFilePath="features/login flow.feature", **ans())
    assert not err, res
    assert writes(ctx)[:2] == ["CreateTest", "CreateTest"]


def test_preview_on_approved_unlisted_project(project):
    ctx = _unlisted_ctx(project)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath="features/login flow.feature")
    assert err and res["error"] == "Unknown Xray project key" and "approval" in res["hint"]
    ctx.preferences.approve("global", ctx.site, "P", False)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P",
                    featureFilePath="features/login flow.feature")
    assert not err and res["preview"] is True and writes(ctx) == []


class MetaJira(RecJira):
    """Jira with create-meta: Severity is required on every item type."""

    def create_meta(self, pk, issue_type):
        return [{"id": "customfield_8", "name": "Severity", "required": True,
                 "schemaType": "option", "allowedValues": ["High", "Low"]}]


class NoFormChannel:
    supports_elicitation = False


def test_discovered_required_fields_are_preview_questions_then_applied(project):
    ctx = ctx_for(project)
    ctx.jira = MetaJira()
    ctx.channel = NoFormChannel()
    args = ans(createTestSet=False, backgrounds="inline")
    res = preview(ctx, LOGIN, **args)
    assert [(q["id"], q["param"]) for q in res["questions"]] == [("customfield_8", "fields")]
    assert writes(ctx) == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True, **args)
    assert not err and res["status"] == "needs_answer" and writes(ctx) == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True, fields={"Severity": "High"}, **args)
    assert not err, res
    sent = [v["jira"]["fields"] for v in ctx.xray.calls_of("CreateTest")]
    assert sent and all(f["customfield_8"] == {"value": "High"} for f in sent)


class FormChannel:
    supports_elicitation = True

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def elicit(self, message, schema):
        self.asked.append(schema)
        return self.answer


def test_apply_elicits_every_item_type_in_one_form(project):
    ctx = ctx_for(project)
    ctx.jira = MetaJira()
    ctx.channel = FormChannel({"test.customfield_8": "High",
                               "precondition.customfield_8": "Low"})
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=LOGIN,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    assert len(ctx.channel.asked) == 1
    assert ctx.channel.asked[0]["required"] == ["test.customfield_8",
                                                "precondition.customfield_8"]
    tests = [v["jira"]["fields"] for v in ctx.xray.calls_of("CreateTest")]
    assert tests and all(f["customfield_8"] == {"value": "High"} for f in tests)
    pre = [v["jira"]["fields"] for v in ctx.xray.calls_of("CreatePrecondition")]
    assert pre and all(f["customfield_8"] == {"value": "Low"} for f in pre)


def test_rule_fields_for_every_new_scenario_are_not_asked(tmp_path):
    class CompJira(RecJira):
        def create_meta(self, pk, issue_type):
            return [{"id": "customfield_9", "name": "Component", "required": True,
                     "schemaType": "option", "allowedValues": []}] \
                if issue_type == "Test" else []

    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    ctx.jira = CompJira()
    path = feature_file(tmp_path, "Feature: F\n  @comp=api\n  Scenario: A\n    Given a\n"
                                  "  @comp=ui\n  Scenario: B\n    Given b\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert res["questions"] == []
    path = feature_file(tmp_path, "Feature: F\n  @comp=api\n  Scenario: A\n    Given a\n"
                                  "  Scenario: B\n    Given b\n", "b.feature")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert [q["id"] for q in res["questions"]] == ["customfield_9"]


class DescJira(RecJira):
    def create_meta(self, pk, issue_type):
        return [{"id": "description", "name": "Description", "required": True,
                 "schemaType": "string", "allowedValues": []}] if issue_type == "Test" else []


def test_scenario_description_wins_and_is_only_asked_for_scenarios_without(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.jira = DescJira()
    ctx.channel = FormChannel({"description": "ASKED"})
    own = feature_file(tmp_path, "Feature: F\n  Scenario: A\n    Own text\n    Given a\n"
                                 "  Scenario: B\n    Own too\n    Given b\n", "own.feature")
    assert preview(ctx, own, **ans(createTestSet=False))["questions"] == []
    path = feature_file(tmp_path, "Feature: F\n  Scenario: A\n    Own text\n    Given a\n"
                                  "  Scenario: B\n    Given b\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, **ans(createTestSet=False))
    assert not err, res
    sent = {v["jira"]["fields"]["summary"]: v["jira"]["fields"].get("description")
            for v in ctx.xray.calls_of("CreateTest")}
    assert sent == {"A": "Own text", "B": "ASKED"} and len(ctx.channel.asked) == 1


def test_partial_jira_config_names_missing_parts_in_not_done(tmp_path):
    ctx = ctx_for(tmp_path, FakeXrayWorld(existing=_existing(labels=["old"]), coverable=["R-1"]), yaml=RULES_YAML)
    ctx.env["JIRA_API_TOKEN"] = "tok"
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @new @REQ_R-1 @comp=backend\n"
                                  "  Scenario: A\n    Given x\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert [nd["what"] for nd in res["notDone"]] == [
        "labels new", "fields Component = backend", "requirement links R-1"]
    for nd in res["notDone"]:
        assert nd["reason"] == "Jira access not configured: site URL, email missing", nd
        assert "the email of the account the token belongs to" in nd["fix"]


# -- questions about key:value tags no rule covers -------------------------------------------

KV = "Feature: F\n  @suite:regression\n  Scenario: A\n    Given a\n"
FIELD_YAML = """
projects:
  P:
    fields:
      test:
        Component: {id: customfield_9, type: option}
"""


def _apply(ctx, path, **args):
    return call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                apply=True, **dict(ans(createTestSet=False), **args))


def test_uncovered_key_value_tag_is_a_preview_question_and_apply_asks(tmp_path):
    ctx = ctx_for(tmp_path)
    path = feature_file(tmp_path, KV)
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagRule.suite"]
    q = res["questions"][0]
    assert q["choices"] == ["label_as_is", "label_value", "test_set", "field", "skip"]
    assert q["param"] == "tagRuleAnswers" and "default" not in q
    assert q["context"]["examples"] == ["@suite:regression"]
    assert q["context"]["suggestedTestSet"] == "Suite {value}"
    assert res["perTest"][0]["labels"] == []  # never a label silently
    res, err = _apply(ctx, path)
    assert not err and res["status"] == "needs_answer", res
    assert [x["id"] for x in res["questions"]] == ["tagRule.suite"]
    assert writes(ctx) == [] and ctx.preferences.get("global", "P").get("tagRules") is None


def test_both_separators_share_one_question_per_key(tmp_path):
    ctx = ctx_for(tmp_path)
    path = feature_file(tmp_path, "Feature: F\n  @suite:smoke\n  Scenario: A\n    Given a\n"
                                  "  @Suite=regression @area=ui\n  Scenario: B\n    Given b\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagRule.suite", "tagRule.area"]
    assert res["questions"][0]["context"]["examples"] == ["@suite:smoke", "@Suite=regression"]
    res, err = _apply(ctx, path, tagRuleAnswers={"suite": {"action": "label_value"},
                                                 "area": {"action": "skip"}})
    assert not err, res
    labels = [c["jira"]["fields"].get("labels") for c in ctx.xray.calls_of("CreateTest")]
    assert labels == [["smoke"], ["regression"]]


@pytest.mark.parametrize("form", ["@suite:regression", "@suite=regression"])
@pytest.mark.parametrize("answer,labels,sets,field,rule", [
    ({"action": "label_as_is"}, ["{tag}"], [], None, {"tag": "suite", "label": "{tag}"}),
    ({"action": "label_value"}, ["regression"], [], None, {"tag": "suite", "label": "{value}"}),
    ({"action": "test_set"}, None, ["Suite regression"], None,
     {"tag": "suite", "testSet": "Suite {value}"}),
    ({"action": "test_set", "testSet": "Run {value}"}, None, ["Run regression"], None,
     {"tag": "suite", "testSet": "Run {value}"}),
    ({"action": "field", "field": "Component"}, None, [],
     ("customfield_9", {"value": "regression"}),
     {"tag": "suite", "field": {"Component": "{value}"}}),
    ({"action": "field", "field": "customfield_9"}, None, [],
     ("customfield_9", {"value": "regression"}),
     {"tag": "suite", "field": {"Component": "{value}"}}),
    ({"action": "skip"}, None, [], None, {"tag": "suite", "skip": True}),
])
def test_each_answer_gives_its_rule_outcome_and_is_remembered(tmp_path, form, answer, labels,
                                                              sets, field, rule):
    ctx = ctx_for(tmp_path, yaml=FIELD_YAML)
    path = feature_file(tmp_path, "Feature: F\n  %s\n  Scenario: A\n    Given a\n" % form)
    res, err = _apply(ctx, path, tagRuleAnswers={"suite": answer})
    assert not err, res
    sent = ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]
    if labels == ["{tag}"]:
        labels = [form[1:]]
    assert sent.get("labels") == labels
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTestSet")] == sets
    if field:
        assert sent[field[0]] == field[1]
    assert ctx.preferences.get("global", "P")["tagRules"] == [rule]
    # another value of the same key later: no question, the rule applies
    path = feature_file(tmp_path, "Feature: F\n  @suite=smoke\n  Scenario: B\n    Given b\n",
                        "b.feature")
    assert qids(preview(ctx, path, **ans(createTestSet=False))) == []


def test_existing_test_is_asked_too_then_labelled(tmp_path):
    """A new key:value tag on an existing test is asked about together with the existing-test
    question, in one round; nothing is written before both are answered."""
    world = FakeXrayWorld(existing=_existing(labels=["old"]))
    world.existing["P-5"]["gherkin"] = "Given x"  # only the tag would change it
    ctx = ctx_for(tmp_path, world)
    ctx.jira = RecJira()
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @suite:regression\n  Scenario: A\n"
                                  "    Given x\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagRule.suite", "existingTests"]
    assert res["existingTests"] == [{"key": "P-5", "scenarioIndex": 0, "scenario": "A",
                                     "stepsChanged": False, "pendingTags": ["suite:regression"]}]
    res, err = _apply(ctx, path)
    assert not err and res["status"] == "needs_answer"
    assert [q["id"] for q in res["questions"]] == ["tagRule.suite", "existingTests"]
    assert writes(ctx) == [] and ctx.jira.edits == []
    res, err = _apply(ctx, path, tagRuleAnswers={"suite": {"action": "label_as_is"}},
                      existingTests="update")
    assert not err, res
    assert ctx.jira.edits == [("P-5", {"labels": ["old", "suite:regression"]})]

def test_settings_or_remembered_rule_covering_the_key_asks_nothing(tmp_path):
    ctx = ctx_for(tmp_path, yaml=RULES_YAML)
    path = feature_file(tmp_path, "Feature: F\n  @suite=x @Area:ui\n  Scenario: A\n    Given a\n")
    assert qids(preview(ctx, path, **ans(createTestSet=False))) == []
    # settings rules and remembered ones both count; settings rules come first
    ctx.preferences.update("global", "P", {"tagRules": [{"tag": "env", "skip": True},
                                                        {"tag": "suite", "skip": True}]})
    path = feature_file(tmp_path, "Feature: F\n  @suite=x @env=dev @os=mac\n  Scenario: A\n"
                                  "    Given a\n", "b.feature")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagRule.os"]
    assert res["perTest"][0]["labels"] == ["x"]
    assert res["choices"]["tagRules"] == {"count": 5, "source": "settings+remembered"}


def test_unmatched_tags_skip_means_no_question(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.preferences.update("global", "P", {"unmatchedTags": "skip"})
    path = feature_file(tmp_path, KV)
    assert qids(preview(ctx, path, **ans(createTestSet=False))) == []


def test_plain_tags_still_go_through_tags_as_labels(tmp_path):
    ctx = ctx_for(tmp_path)
    path = feature_file(tmp_path, "Feature: F\n  @smoke @suite:x\n  Scenario: A\n    Given a\n")
    res = preview(ctx, path, createTestSet=False, aiLabel="")
    assert qids(res) == ["tagsAsLabels", "tagRule.suite"]
    ctx.xray.calls.clear()
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, createTestSet=False, aiLabel="")
    assert err and qids(res) == ["tagsAsLabels", "tagRule.suite"]
    assert "tagRule.suite" in res["error"] and ctx.xray.calls == []
    res, err = _apply(ctx, path, tagsAsLabels=False,
                      tagRuleAnswers={"suite": {"action": "label_as_is"}})
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["suite:x"]


def test_elicitation_form_asks_every_key_without_default_and_remembers(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.channel = FormChannel({"tagRule.suite": "test_set", "tagRule.suite.testSet": "",
                               "tagRule.area": "label_value"})
    path = feature_file(tmp_path, "Feature: F\n  @suite:regression @area=ui\n  Scenario: A\n"
                                  "    Given a\n")
    res, err = _apply(ctx, path)
    assert not err, res
    schema = ctx.channel.asked[0]
    assert schema["required"] == ["tagRule.suite", "tagRule.area"]
    assert schema["properties"]["tagRule.suite"]["enum"] == [
        "label_as_is", "label_value", "test_set", "field", "skip"]
    assert all("default" not in p for p in schema["properties"].values())
    assert all(p["type"] == "string" for p in schema["properties"].values())
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTestSet")] == [
        "Suite regression"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["ui"]
    assert ctx.preferences.get("global", "P")["tagRules"] == [
        {"tag": "suite", "testSet": "Suite {value}"}, {"tag": "area", "label": "{value}"}]


@pytest.mark.parametrize("answer", [None, {"tagRule.suite": "field"}, {"tagRule.suite": "x"}])
def test_declined_or_incomplete_form_writes_and_remembers_nothing(tmp_path, answer):
    ctx = ctx_for(tmp_path)
    ctx.channel = FormChannel(answer)
    res, err = _apply(ctx, feature_file(tmp_path, KV))
    assert err and writes(ctx) == [], res
    assert "tagRules" not in ctx.preferences.get("global", "P")


@pytest.mark.parametrize("answers,msg", [
    ("label", "expected object"),
    ({"suite": "skip"}, "must be an object"),
    ({"suite": {"action": "nope"}}, "action must be one of"),
    ({"suite": {"action": "field"}}, "needs `field`"),
    ({"suite": {"action": "skip", "testSet": "X"}}, "only goes with action test_set"),
    ({"suite": {"action": "skip", "extra": "1"}}, "unknown key"),
    ({"suite": {"action": "field", "field": "Nope"}}, "unknown field"),
    ({"suite": {"action": "test_set", "testSet": "{bad}"}}, "unknown placeholder"),
])
def test_invalid_tag_rule_answers_are_refused_before_calls(tmp_path, answers, msg):
    ctx = ctx_for(tmp_path)
    res, err = _apply(ctx, feature_file(tmp_path, KV), tagRuleAnswers=answers)
    assert err and msg in res["error"], res
    assert writes(ctx) == [] and "tagRules" not in ctx.preferences.get("global", "P")


class TypedMetaJira(RecJira):
    def create_meta(self, pk, issue_type):
        return [{"id": "customfield_44", "name": "Suite", "schemaType": "option"}]


def _existing_with(field_id):
    return {"P-5": {"issueId": "55", "testType": {"name": "Cucumber"},
                    "jira": {"key": "P-5", "summary": "A", "labels": [], field_id: None}}}


def test_field_id_on_existing_test_converts_by_create_metadata(tmp_path):
    ctx = ctx_for(tmp_path, FakeXrayWorld(existing=_existing_with("customfield_44")))
    ctx.jira = TypedMetaJira()
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @suite:regression\n  Scenario: A\n"
                                  "    Given x\n")
    res, err = _apply(ctx, path, existingTests="update", tagRuleAnswers={"suite": {"action": "field",
                                                           "field": "customfield_44"}})
    assert not err, res
    assert ctx.jira.edits == [("P-5", {"customfield_44": {"value": "regression"}})]
    assert res["fieldsApplied"] == [{"test": "P-5", "fields": ["customfield_44"]}]


def test_field_id_without_metadata_on_existing_test_is_not_done_never_written(tmp_path):
    ctx = ctx_for(tmp_path, FakeXrayWorld(existing=_existing_with("customfield_44")))
    ctx.jira = RecJira()  # create metadata without the field
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @suite:regression\n  Scenario: A\n"
                                  "    Given x\n")
    res, err = _apply(ctx, path, existingTests="update", tagRuleAnswers={"suite": {"action": "field",
                                                           "field": "customfield_44"}})
    assert not err, res
    assert ctx.jira.edits == []
    assert [(n["what"], "type is unknown" in n["reason"]) for n in res["notDone"]] == [
        ("field customfield_44 = regression", True)]


def test_preview_counts_rules_answered_in_the_call(tmp_path):
    ctx = ctx_for(tmp_path)
    res = preview(ctx, feature_file(tmp_path, KV), tagRuleAnswers={"suite": {"action": "skip"}},
                  **ans(createTestSet=False))
    assert res["questions"] == [] and res["choices"]["tagRules"]["count"] == 1
    assert res["tagSummary"]["handledByRules"] == ["suite:regression"]
    assert "tagRules" not in ctx.preferences.get("global", "P")


def test_form_field_without_a_name_is_refused_for_that_entry(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.channel = FormChannel({"tagRule.suite": "field", "tagRule.suite.field": " "})
    res, err = _apply(ctx, feature_file(tmp_path, KV))
    assert err and "@suite:regression" in res["error"] and "no Jira field" in res["error"]
    assert res["tagKeys"] == ["suite"] and writes(ctx) == []


def test_answer_keys_colliding_case_insensitively_are_refused(tmp_path):
    ctx = ctx_for(tmp_path)
    res, err = _apply(ctx, feature_file(tmp_path, KV),
                      tagRuleAnswers={"suite": {"action": "skip"}, "Suite": {"action": "skip"}})
    assert err and "answered twice" in res["error"] and writes(ctx) == []


# -- issue-key tags are looked up; the edge-case file previews without refusal ---------------

def test_key_shaped_tag_xray_cannot_place_is_asked_then_answered(tmp_path):
    path = feature_file(tmp_path, "Feature: F\n  @ISO-8601 @smoke\n  Scenario: A\n    Given a\n")
    ctx = ctx_for(tmp_path)
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["keyTag.ISO-8601"]
    q = res["questions"][0]
    assert q["choices"] == ["link", "label", "ignore"] and "default" not in q
    assert q["param"] == "keyTagAnswers"
    assert q["context"]["where"] == [{"level": "scenario", "name": "A"}]
    assert res["tagSummary"]["keyTags"][0]["meaning"] == "asked"
    assert res["perTest"][0]["labels"] == ["smoke"]  # nothing from the open question
    res, err = _apply(ctx, path)
    assert not err and res["status"] == "needs_answer"
    assert [x["id"] for x in res["questions"]] == ["keyTag.ISO-8601"] and writes(ctx) == []
    res, err = _apply(ctx, path, keyTagAnswers={"ISO-8601": "label"})
    assert not err, res
    jira = ctx.xray.calls_of("CreateTest")[-1]["jira"]
    assert jira["fields"]["labels"] == ["ISO-8601", "smoke"] and "update" not in jira
    res, err = _apply(ctx, path, keyTagAnswers={"ISO-8601": "ignore"})
    assert not err and ctx.xray.calls_of("CreateTest")[-1]["jira"]["fields"]["labels"] == ["smoke"]
    res, err = _apply(ctx, path, keyTagAnswers={"ISO-8601": "maybe"})
    assert err and "keyTagAnswers" in res["error"]
    assert ctx.preferences.get("global", "P").get("keyTagAnswers") is None  # never remembered


def test_link_answer_refused_by_jira_creates_the_test_without_it(tmp_path):
    path = feature_file(tmp_path, "@R-1\nFeature: F\n  @ISO-8601\n  Scenario: A\n    Given a\n")
    world = FakeXrayWorld(coverable=["R-1"])
    base = world.responses()
    create = base["CreateTest"]

    def create_test(v):
        links = [l["add"]["outwardIssue"]["key"] for l in
                 (v["jira"].get("update") or {}).get("issuelinks", [])]
        if "ISO-8601" in links:
            raise ToolError("Xray GraphQL error: issuelinks: Issue 'ISO-8601' does not exist")
        return create(v)
    base["CreateTest"] = create_test
    ctx = make_context(YAML, responses=base)
    ctx.env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    res, err = _apply(ctx, path, keyTagAnswers={"ISO-8601": "link"})
    assert not err, res
    first, second = ctx.xray.calls_of("CreateTest")
    assert second["jira"]["update"] == {"issuelinks": [
        {"add": {"type": {"name": "Test"}, "outwardIssue": {"key": "R-1"}}}]}
    assert res["created"] == ["P-101"]
    assert [n["what"] for n in res["notDone"]] == ["requirement link ISO-8601"]
    assert "does not exist" in res["notDone"][0]["reason"]


def test_key_tag_questions_join_the_elicitation_form(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.channel = FormChannel({"tagRule.suite": "label_value", "keyTag.ISO-8601": "label"})
    path = feature_file(tmp_path, "Feature: F\n  @ISO-8601 @suite:x\n  Scenario: A\n    Given a\n")
    res, err = _apply(ctx, path)
    assert not err, res
    schema = ctx.channel.asked[0]
    assert schema["required"] == ["tagRule.suite", "keyTag.ISO-8601"]
    assert schema["properties"]["keyTag.ISO-8601"]["enum"] == ["link", "label", "ignore"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["ISO-8601", "x"]
    assert len(ctx.channel.asked) == 1


def test_tag_naming_a_test_of_another_project_creates_a_new_test_with_a_note(tmp_path):
    path = feature_file(tmp_path, "Feature: F\n  @Q-5\n  Scenario: A\n    Given a\n"
                                  "  @TEST_Q-5\n  Scenario: B\n    Given b\n")
    q5 = {"issueId": "5", "testType": {"name": "Cucumber"},
          "jira": {"key": "Q-5", "summary": "A", "labels": []}}
    ctx = ctx_for(tmp_path, FakeXrayWorld(existing={"Q-5": q5}))
    res = preview(ctx, path, **ans(createTestSet=False))
    assert [a["kind"] for a in res["actions"]] == ["create_test", "skip_test"]
    assert "labels" not in res["actions"][0]
    assert [n["item"] for n in res["notDone"]] == ["scenario 'A' (line 3)",
                                                   "scenario 'B' (line 6)"]
    assert res["notDone"][1]["what"] == "scenario not imported (tag @TEST_Q-5)"
    res, err = _apply(ctx, path)
    assert not err, res
    assert writes(ctx) == ["CreateTest"] and len(res["notDone"]) == 2


def test_requirement_xray_knows_is_linked_without_asking(tmp_path):
    path = feature_file(tmp_path, "@R-1\nFeature: F\n  Scenario: A\n    Given a\n")
    ctx = ctx_for(tmp_path, FakeXrayWorld(coverable=["R-1"]))
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == []
    res, err = _apply(ctx, path)
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["update"] == {"issuelinks": [
        {"add": {"type": {"name": "Test"}, "outwardIssue": {"key": "R-1"}}}]}


def test_edge_case_file_previews_without_refusal(tmp_path):
    """The user's edge-case file: Xray returns no rows for ISO-8601 (verified live), so it is
    asked about in the same round as the other questions."""
    shutil.copy(FIXTURES / "edge_cases.feature", tmp_path / "edge.feature")
    ctx = ctx_for(tmp_path)
    res = preview(ctx, "edge.feature")
    assert qids(res) == ["createTestSet", "backgrounds", "tagsAsLabels", "aiLabel",
                         "aiLabelScope", "tagRule.suite", "tagRule.team", "tagRule.priority",
                         "tagRule.area", "keyTag.ISO-8601"]
    iso_q = res["questions"][-1]
    assert iso_q["context"]["where"] == [
        {"level": "scenario", "name": "Code expiry time is shown in ISO format"}]
    assert res["tagSummary"]["keyTags"] == [{"tag": "ISO-8601", "meaning": "asked", "note": (
        "Xray doesn't know ISO-8601 as a test or a requirement (it may be another kind of "
        "issue, or not an issue key at all); question keyTag.ISO-8601")}]
    assert "doesn't know ISO-8601 as a test or a requirement" in iso_q["question"]
    assert "exist" not in iso_q["question"]
    tests = [a for a in res["actions"] if a["kind"] == "create_test"]
    # the same scenario name under two Rules: told apart by the Rule name
    assert [a["summary"] for a in tests][-3:] == [
        "Two-factor sign-in: Sign in", "Code expiry time is shown in ISO format",
        "Passwordless sign-in: Sign in"]
    assert len(tests) == 8 and res["notDone"] == []
    assert "possibleDuplicates" not in res
    assert writes(ctx) == []
    res = preview(ctx, "edge.feature", keyTagAnswers={"ISO-8601": "label"})
    iso = next(a for a in res["actions"] if a["summary"].startswith("Code expiry"))
    assert "ISO-8601" in iso["labels"] and "keyTag.ISO-8601" not in qids(res)


# -- existing tests the import would change are asked about first --------------------------

TWO_EXISTING = ("Feature: F\n  @TEST_P-5 @new @REQ_R-1\n  Scenario: A\n    Given a\n"
                "  @P-6\n  Scenario: B\n    Given b\n  Scenario: C\n    Given c\n")


def _two_world(gherkin_b="Given old"):
    rows = dict(_existing(labels=["old"]), **_existing("P-6", "66"))
    rows["P-5"]["gherkin"] = "Given a"           # steps as in the file
    rows["P-6"]["gherkin"] = gherkin_b
    rows["P-6"]["testSets"] = {"results": [{"issueId": "70"}]}  # already in the Test Set
    return FakeXrayWorld(existing=rows)


def _with_set(ctx):
    ctx.xray.responses["FindTestSets"] = {"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "F"}}]}}


def test_preview_lists_what_would_change_on_each_existing_test(tmp_path):
    ctx = ctx_for(tmp_path, _two_world())
    _with_set(ctx)
    ctx.jira = RecJira()
    path = feature_file(tmp_path, TWO_EXISTING)
    res = preview(ctx, path, **ans())
    assert qids(res) == ["existingTests"]
    assert res["existingTests"] == [
        {"key": "P-5", "scenarioIndex": 0, "scenario": "A", "stepsChanged": False,
         "labelsAdded": ["new"], "links": ["R-1"], "testSets": ["F"]},
        {"key": "P-6", "scenarioIndex": 1, "scenario": "B", "stepsChanged": True,
         "stepsDiff": {"added": 1, "removed": 1, "lines": ["- Given old", "+ Given b"]}}]
    q = res["questions"][0]
    assert q["choices"] == ["update", "keep", "skip"] and "default" not in q
    assert ("P-5 (A): named by a key tag; steps unchanged; labels +new; requirement links "
            "+R-1; joins Test Set(s) F") in q["question"]
    assert "P-6 (B): named by a key tag; steps changed (+1/-1 line(s))" in q["question"]
    assert "create" not in q["choices"]
    assert writes(ctx) == []


def test_existing_tests_answers_update_keep_skip_and_overrides(tmp_path):
    path = feature_file(tmp_path, TWO_EXISTING)

    def run(**answers):
        ctx = ctx_for(tmp_path, _two_world())
        _with_set(ctx)
        ctx.jira = RecJira()
        res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                        apply=True, **ans(), **answers)
        assert not err, res
        return ctx, res

    ctx, res = run(existingTests="update")
    assert res["updated"] == ["P-5", "P-6"] and ctx.jira.edits == [("P-5", {"labels": ["old", "new"]})]
    assert ctx.xray.calls_of("AddTestsToTestSet")[0]["testIssueIds"] == ["55", "66", "101"]
    ctx, res = run(existingTests="keep")
    assert res["updated"] == [] and ctx.jira.edits == [] and ctx.jira.links == []
    assert "UpdateGherkin" not in writes(ctx) and res["unchanged"] == ["P-5", "P-6"]
    assert res["tests"] == [{"index": 0, "key": "P-5"}, {"index": 1, "key": "P-6"},
                            {"index": 2, "key": "P-101"}]
    assert ctx.xray.calls_of("AddTestsToTestSet")[0]["testIssueIds"] == ["101"]
    ctx, res = run(existingTests="skip")
    assert res["updated"] == [] and "unchanged" not in res
    assert res["tests"] == [{"index": 2, "key": "P-101"}]
    ctx, res = run(existingTests="update", existingTestOverrides={"p-6": "skip"})
    assert res["updated"] == ["P-5"]
    # overrides alone answer every test: no question
    ctx, res = run(existingTestOverrides={"P-5": "keep", "P-6": "update"})
    assert res["updated"] == ["P-6"] and res["unchanged"] == ["P-5"]


def test_existing_tests_question_unanswered_on_apply_writes_nothing(tmp_path):
    ctx = ctx_for(tmp_path, _two_world())
    path = feature_file(tmp_path, TWO_EXISTING)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, existingTestOverrides={"P-5": "keep"}, **ans())
    assert not err and res["status"] == "needs_answer"
    assert [t["key"] for t in res["questions"][0]["context"]["tests"]] == ["P-6"]
    assert writes(ctx) == []
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True, existingTests="maybe", **ans())
    assert err and "existingTests" in res["error"]


def test_existing_test_with_nothing_to_change_is_only_mapped(tmp_path):
    ctx = ctx_for(tmp_path, _two_world(gherkin_b="Given b"))
    path = feature_file(tmp_path, "Feature: F\n  @P-6\n  Scenario: B\n    Given b\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == [] and "existingTests" not in res
    assert res["actions"][0]["kind"] == "keep_test"
    res, err = _apply(ctx, path)
    assert not err, res
    assert writes(ctx) == [] and res["unchanged"] == ["P-6"]


def test_existing_tests_question_joins_the_elicitation_form(tmp_path):
    ctx = ctx_for(tmp_path, _two_world())
    ctx.channel = FormChannel({"tagRule.suite": "label_value", "existingTests": "keep",
                               "existingTests.P-6": "update"})
    path = feature_file(tmp_path, TWO_EXISTING.replace("@P-6", "@P-6 @suite:smoke"))
    res, err = _apply(ctx, path)
    assert not err, res
    props = ctx.channel.asked[0]["properties"]
    assert ctx.channel.asked[0]["required"] == ["tagRule.suite"]
    assert props["existingTests"]["enum"] == ["update", "keep", "skip"]
    # without Jira and a Test Set, nothing would change on P-5: not asked, only mapped
    assert "existingTests.P-6" in props and "existingTests.P-5" not in props
    assert res["updated"] == ["P-6"] and res["unchanged"] == ["P-5"]
    assert len(ctx.channel.asked) == 1


def test_apply_with_open_choices_asks_existing_tests_in_the_same_round(tmp_path):
    ctx = ctx_for(tmp_path, _two_world())
    path = feature_file(tmp_path, TWO_EXISTING)
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True)
    assert err and "existingTests" in res["error"]
    assert [q["id"] for q in res["questions"]][-1] == "existingTests"
    assert writes(ctx) == []


def test_steps_differing_only_in_line_endings_or_trailing_spaces_are_unchanged(tmp_path):
    ctx = ctx_for(tmp_path, _two_world(gherkin_b="Given b   \r\n"))
    path = feature_file(tmp_path, "Feature: F\n  @P-6\n  Scenario: B\n    Given b\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert "existingTests" not in res and res["actions"][0]["kind"] == "keep_test"


@pytest.mark.parametrize("status", [500, 429])
def test_failed_key_lookup_stops_the_import_before_any_write(tmp_path, status):
    world = FakeXrayWorld()
    base = world.responses()
    plain = base["SearchTests"]

    def search(v):
        if v["jql"].startswith("issue"):
            raise ToolError("Xray request failed with HTTP %d" % status, status=status)
        return plain(v)
    base["SearchTests"] = search
    ctx = make_context(YAML, responses=base)
    ctx.env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    path = feature_file(tmp_path, "Feature: F\n  @P-5\n  Scenario: A\n    Given a\n")
    res, err = _apply(ctx, path)
    assert err and ("HTTP %d" % status) in res["error"]
    assert writes(ctx) == []


def test_refused_unverified_link_on_an_existing_test_is_not_done_not_a_failure(tmp_path):
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @REQ_R-404\n  Scenario: A\n"
                                  "    Given a\n")
    ctx = ctx_for(tmp_path, _existing_world())

    class Refusing(RecJira):
        def link_requirement(self, key, req):
            raise ToolError("Jira request for link failed with HTTP 404: Issue does not exist",
                            status=404)
    ctx.jira = Refusing()
    res, err = _apply(ctx, path, existingTests="update")
    assert not err, res
    assert res["updated"] == ["P-5"]
    assert [n["what"] for n in res["notDone"]] == ["requirement link R-404"]


def test_open_key_tag_on_an_unchanged_existing_test_asks_existing_tests_in_the_same_round(
        tmp_path):
    ctx = ctx_for(tmp_path, _two_world(gherkin_b="Given b"))  # P-6's steps as in the file
    path = feature_file(tmp_path, "Feature: F\n  @P-6 @ISO-8601\n  Scenario: B\n    Given b\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["keyTag.ISO-8601", "existingTests"]
    assert res["existingTests"][0]["pendingTags"] == ["ISO-8601"]
    res, err = _apply(ctx, path)
    assert not err and res["status"] == "needs_answer"
    assert [q["id"] for q in res["questions"]] == ["keyTag.ISO-8601", "existingTests"]


@pytest.mark.parametrize("error", [
    ToolError("Xray request failed with HTTP 500", status=500),
    ToolError("Network error contacting xray.cloud.getxray.app (timeout)")])
def test_ambiguous_create_failure_with_unconfirmed_links_is_never_retried(tmp_path, error):
    path = feature_file(tmp_path, "Feature: F\n  @ISO-8601\n  Scenario: A\n    Given a\n")
    world = FakeXrayWorld()
    base = world.responses()

    def create_test(v):
        raise error
    base["CreateTest"] = create_test
    ctx = make_context(YAML, responses=base)
    ctx.env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    res, err = _apply(ctx, path, keyTagAnswers={"ISO-8601": "link"})
    assert err and error.message in res["error"]
    assert len(ctx.xray.calls_of("CreateTest")) == 1  # no second create: no duplicate


def test_transient_link_failure_on_an_existing_test_is_not_taken_as_a_refusal(tmp_path):
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @REQ_R-404\n  Scenario: A\n"
                                  "    Given a\n")
    ctx = ctx_for(tmp_path, _existing_world())

    class Flaky(RecJira):
        def link_requirement(self, key, req):
            raise ToolError("Jira request for link failed with HTTP 503", status=503)
    ctx.jira = Flaky()
    res, err = _apply(ctx, path, existingTests="update")
    assert err and "stopped while updating test P-5" in res["error"]  # partial failure
    assert res["updated"] == ["P-5"] and "503" in json.dumps(res)


# -- round 3: tag-rule values, system fields, repeated names, summaries, name matches --------

class TeamMetaJira(RecJira):
    """Create metadata: a select list, a multi-select and an Atlassian Team field."""

    def create_meta(self, pk, issue_type):
        return [{"id": "customfield_44", "name": "Severity", "schemaType": "option",
                 "allowedValues": ["High", "Low"]},
                {"id": "customfield_45", "name": "Areas", "schemaType": "array",
                 "schemaItems": "option", "allowedValues": ["Checkout", "Search"]},
                {"id": "customfield_50", "name": "Team", "schemaType": "team"},
                {"id": "priority", "name": "Priority", "schemaType": "priority",
                 "allowedValues": ["Highest", "High", "Low"]}]


def _team_rules(ctx, *rules):
    ctx.preferences.update("global", "P", {"tagRules": list(rules)})


def test_option_values_are_matched_by_name_with_create_metadata(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.jira = TeamMetaJira()
    _team_rules(ctx, {"tag": "sev", "field": {"customfield_44": "{value}"}},
                {"tag": "areas", "field": {"customfield_45": "{value}"}},
                {"tag": "prio", "field": {"priority": "{value}"}})
    path = feature_file(tmp_path, "Feature: F\n  @sev=high @areas=checkout @prio=HIGH\n"
                                  "  Scenario: A\n    Given a\n")
    assert qids(preview(ctx, path, **ans(createTestSet=False))) == []
    res, err = _apply(ctx, path)
    assert not err, res
    sent = ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]
    assert sent["customfield_44"] == {"value": "High"}
    assert sent["customfield_45"] == [{"value": "Checkout"}]
    assert sent["priority"] == {"name": "High"}


def test_team_value_is_asked_once_remembered_in_the_rule_and_asked_again_for_a_new_value(
        tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.jira = TeamMetaJira()
    ctx.channel = NoFormChannel()
    _team_rules(ctx, {"tag": "team", "field": {"customfield_50": "{value}"}})
    path = feature_file(tmp_path, "Feature: F\n  @team=checkout\n  Scenario: A\n    Given a\n"
                                  "  @team=checkout\n  Scenario: B\n    Given b\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagValue.customfield_50.checkout"]
    q = res["questions"][0]
    assert q["param"] == "tagValueAnswers" and "choices" not in q and "default" not in q
    assert "What is `checkout` for customfield_50?" in q["question"]
    assert len(q["context"]["scenarios"]) == 2
    res, err = _apply(ctx, path)
    assert not err and res["status"] == "needs_answer" and writes(ctx) == []
    assert [x["id"] for x in res["questions"]] == ["tagValue.customfield_50.checkout"]
    res, err = _apply(ctx, path, tagValueAnswers={"customfield_50": {"checkout": "team-7"}})
    assert not err, res
    assert [c["jira"]["fields"]["customfield_50"] for c in ctx.xray.calls_of("CreateTest")] \
        == ["team-7", "team-7"]
    assert ctx.preferences.get("global", "P")["tagRules"] == [
        {"tag": "team", "field": {"customfield_50": "{value}"}, "values": {"checkout": "team-7"}}]
    # the same value later: not asked; a new value: asked
    path = feature_file(tmp_path, "Feature: F\n  @team=Checkout\n  Scenario: C\n    Given c\n"
                                  "  @team=search\n  Scenario: D\n    Given d\n", "b.feature")
    assert qids(preview(ctx, path, **ans(createTestSet=False))) == [
        "tagValue.customfield_50.search"]


def test_custom_field_without_the_jira_token_is_asked_not_sent_as_text(tmp_path):
    ctx = ctx_for(tmp_path)
    path = feature_file(tmp_path, KV)
    res = preview(ctx, path, tagRuleAnswers={"suite": {"action": "field",
                                                       "field": "customfield_44"}},
                  **ans(createTestSet=False))
    assert qids(res) == ["tagValue.customfield_44.regression"]
    assert "unknown without the Jira API token" in res["questions"][0]["context"]["reason"]
    res, err = _apply(ctx, path, tagRuleAnswers={"suite": {"action": "field",
                                                           "field": "customfield_44"}},
                      tagValueAnswers={"customfield_44": {"regression": '{"value": "Reg"}'}})
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["customfield_44"] == {
        "value": "Reg"}
    assert ctx.preferences.get("global", "P")["tagRules"] == [
        {"tag": "suite", "field": {"customfield_44": "{value}"},
         "values": {"regression": {"value": "Reg"}}}]


def test_option_without_a_matching_name_is_asked(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.jira = TeamMetaJira()
    _team_rules(ctx, {"tag": "sev", "field": {"customfield_44": "{value}"}})
    path = feature_file(tmp_path, "Feature: F\n  @sev=urgent\n  Scenario: A\n    Given a\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagValue.customfield_44.urgent"]
    assert "no option named 'urgent'" in res["questions"][0]["context"]["reason"]


def test_field_answer_then_value_question_follow_in_the_form_before_any_write(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.jira = TeamMetaJira()

    class TwoForms(FormChannel):
        def elicit(self, message, schema):
            self.asked.append(schema)
            return ({"tagRule.team": "field", "tagRule.team.field": "customfield_50"}
                    if len(self.asked) == 1 else {"tagValue.customfield_50.checkout": "t-1"})
    ctx.channel = TwoForms(None)
    path = feature_file(tmp_path, "Feature: F\n  @team=checkout\n  Scenario: A\n    Given a\n")
    res, err = _apply(ctx, path)
    assert not err, res
    assert len(ctx.channel.asked) == 2
    assert ctx.channel.asked[1]["required"] == ["tagValue.customfield_50.checkout"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["customfield_50"] == "t-1"
    assert ctx.preferences.get("global", "P")["tagRules"] == [
        {"tag": "team", "field": {"customfield_50": "{value}"}, "values": {"checkout": "t-1"}}]


def test_value_question_joins_the_tag_questions_in_one_form(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.jira = TeamMetaJira()
    _team_rules(ctx, {"tag": "team", "field": {"customfield_50": "{value}"}})
    ctx.channel = FormChannel({"tagRule.suite": "skip",
                               "tagValue.customfield_50.checkout": "t-2"})
    path = feature_file(tmp_path, "Feature: F\n  @team=checkout @suite=x\n  Scenario: A\n"
                                  "    Given a\n")
    res, err = _apply(ctx, path)
    assert not err, res
    assert len(ctx.channel.asked) == 1
    assert ctx.channel.asked[0]["required"] == ["tagRule.suite",
                                                "tagValue.customfield_50.checkout"]


SETTINGS_TEAM = """
projects:
  P:
    fields:
      test:
        Owner: {id: customfield_60, type: user}
    featureImport:
      tagRules:
        - tag: owner
          field: {Owner: "{value}"}
"""


def test_settings_rule_value_answer_is_used_but_not_stored(tmp_path):
    ctx = ctx_for(tmp_path, yaml=SETTINGS_TEAM)
    path = feature_file(tmp_path, "Feature: F\n  @owner=alice\n  Scenario: A\n    Given a\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagValue.Owner.alice"]
    res, err = _apply(ctx, path, tagValueAnswers={"Owner": {"alice": "acc-1"}})
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["customfield_60"] == {
        "accountId": "acc-1"}
    assert any("settings file" in w and "values" in w for w in res["warnings"])
    assert "tagRules" not in ctx.preferences.get("global", "P")


def test_settings_rule_with_a_value_map_asks_nothing(tmp_path):
    yaml = SETTINGS_TEAM + "          values: {alice: acc-9}\n"
    ctx = ctx_for(tmp_path, yaml=yaml)
    path = feature_file(tmp_path, "Feature: F\n  @owner=Alice\n  Scenario: A\n    Given a\n")
    assert qids(preview(ctx, path, **ans(createTestSet=False))) == []
    res, err = _apply(ctx, path)
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["customfield_60"] == {
        "accountId": "acc-9"}


def test_call_field_skips_the_value_question(tmp_path):
    ctx = ctx_for(tmp_path, yaml=SETTINGS_TEAM)
    path = feature_file(tmp_path, "Feature: F\n  @owner=alice\n  Scenario: A\n    Given a\n")
    res = preview(ctx, path, fields={"Owner": "acc-3"}, **ans(createTestSet=False))
    assert qids(res) == []
    res, err = _apply(ctx, path, fields={"Owner": "acc-3"})
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["customfield_60"] == {
        "accountId": "acc-3"}


@pytest.mark.parametrize("answers,msg", [
    ("x", "expected object"),
    ({"Owner": "x"}, "must be an object {tag value"),
    ({"Owner": {"alice": " "}}, "non-empty string"),
])
def test_invalid_tag_value_answers_are_refused(tmp_path, answers, msg):
    ctx = ctx_for(tmp_path, yaml=SETTINGS_TEAM)
    path = feature_file(tmp_path, "Feature: F\n  @owner=alice\n  Scenario: A\n    Given a\n")
    res, err = _apply(ctx, path, tagValueAnswers=answers)
    assert err and msg in res["error"] and writes(ctx) == []


@pytest.mark.parametrize("field,expected_id,sent", [
    ("Priority", "priority", {"name": "high"}),
    ("components", "components", [{"name": "high"}]),
    ("Fix versions", "fixVersions", [{"name": "high"}]),
])
def test_system_fields_are_tag_rule_targets_without_the_jira_token(tmp_path, field, expected_id,
                                                                   sent):
    ctx = ctx_for(tmp_path)
    path = feature_file(tmp_path, "Feature: F\n  @prio:high\n  Scenario: A\n    Given a\n")
    res, err = _apply(ctx, path, tagRuleAnswers={"prio": {"action": "field", "field": field}})
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"][expected_id] == sent
    assert ctx.preferences.get("global", "P")["tagRules"] == [
        {"tag": "prio", "field": {expected_id: "{value}"}}]


def test_system_field_in_a_settings_rule_is_accepted(tmp_path):
    yaml = ("projects:\n  P:\n    featureImport:\n      tagRules:\n        - tag: prio\n"
            "          field: {priority: \"{value}\"}\n")
    ctx = ctx_for(tmp_path, yaml=yaml)
    path = feature_file(tmp_path, "Feature: F\n  @prio:Low\n  Scenario: A\n    Given a\n")
    res, err = _apply(ctx, path)
    assert not err, res
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["priority"] == {"name": "Low"}


def test_repeated_scenario_names_are_told_apart_only_when_they_collide(tmp_path):
    ctx = ctx_for(tmp_path)
    path = feature_file(tmp_path, "Feature: F\n  Scenario: Sign in\n    Given a\n"
                                  "  Scenario: sign  in\n    Given b\n"
                                  "  Scenario: Other\n    Given c\n"
                                  "  Rule: R\n    Scenario: Other\n      Given d\n"
                                  "    Scenario: Other\n      Given e\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert [a["summary"] for a in res["actions"]] == [
        "F: Sign in", "F: sign  in (2)", "F: Other", "R: Other", "R: Other (2)"]
    assert res["perTest"][0]["scenarioName"] == "Sign in"
    res, err = _apply(ctx, path)
    assert not err, res
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTest")][:2] == [
        "F: Sign in", "F: sign  in (2)"]


def test_summaries_rename_created_tests_and_are_ignored_for_existing_ones(tmp_path):
    world = FakeXrayWorld(existing=_existing())
    world.existing["P-5"]["gherkin"] = "Given x"
    ctx = ctx_for(tmp_path, world)
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5\n  Scenario: A\n    Given x\n"
                                  "  Scenario: B\n    Given b\n  Scenario: C\n    Given c\n")
    renames = {"0": "Renamed A", "B": "Better B", "2": "Third"}
    res = preview(ctx, path, summaries=renames, **ans(createTestSet=False))
    assert [a["summary"] for a in res["actions"]] == ["A", "Better B", "Third"]
    assert any("summaries[0]" in w and "P-5" in w for w in res["warnings"])
    res, err = _apply(ctx, path, summaries=renames)
    assert not err, res
    assert [c["jira"]["fields"]["summary"] for c in ctx.xray.calls_of("CreateTest")] == [
        "Better B", "Third"]


@pytest.mark.parametrize("renames,msg", [
    ([1], "expected object"),
    ({"9": "X"}, "out of range"),
    ({"Nope": "X"}, "no scenario has this name"),
    ({"Sign in": "X"}, "use the index"),
    ({"0": " "}, "non-empty single-line"),
    ({"0": "a\nb"}, "non-empty single-line"),
    ({"0": "x" * 256}, "longer than 255"),
    ({"0": "Other"}, "also the summary of scenario 2"),
    ({"0": "X", "F: Sign in": "Y"}, "renamed twice"),
])
def test_invalid_summaries_are_refused_before_calls(tmp_path, renames, msg):
    ctx = ctx_for(tmp_path)
    path = feature_file(tmp_path, "Feature: F\n  Scenario: Sign in\n    Given a\n"
                                  "  Scenario: Sign in\n    Given b\n"
                                  "  Scenario: Other\n    Given c\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    summaries=renames, **ans(createTestSet=False))
    assert err and msg in res["error"], res
    assert ctx.xray.calls == []


def _named_world(gherkin="Given old", test_type="Cucumber"):
    world = FakeXrayWorld(dup_summaries=["A"], existing={"P-1": {
        "issueId": "1", "testType": {"name": test_type}, "gherkin": gherkin,
        "jira": {"key": "P-1", "summary": "A", "labels": []}}})
    return world


NAMED = "Feature: F\n  Scenario: A\n    Given a\n"


def test_scenario_matching_an_existing_test_by_name_is_asked_like_an_existing_test(tmp_path):
    ctx = ctx_for(tmp_path, _named_world())
    path = feature_file(tmp_path, NAMED)
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["existingTests"] and "possibleDuplicates" not in res
    assert res["existingTests"] == [{
        "key": "P-1", "scenarioIndex": 0, "scenario": "A", "stepsChanged": True,
        "matchedBy": "name",
        "stepsDiff": {"added": 1, "removed": 1, "lines": ["- Given old", "+ Given a"]}}]
    q = res["questions"][0]
    assert q["choices"] == ["update", "keep", "create", "skip"]
    assert "P-1 (A): matched by name; steps changed (+1/-1 line(s))" in q["question"]
    res, err = _apply(ctx, path)
    assert not err and res["status"] == "needs_answer" and writes(ctx) == []

    def run(**answers):
        c = ctx_for(tmp_path, _named_world())
        r, e = _apply(c, path, **answers)
        assert not e, r
        return c, r
    c, r = run(existingTests="update")
    assert r["updated"] == ["P-1"] and "CreateTest" not in writes(c)
    assert c.xray.calls_of("UpdateGherkin")[0]["issueId"] == "1"
    c, r = run(existingTests="keep")
    assert r["unchanged"] == ["P-1"] and writes(c) == []
    for answers in ({"existingTests": "create"}, {"existingTestOverrides": {"P-1": "create"}},
                    {"createDuplicates": [0]}):
        c, r = run(**answers)
        assert [x["jira"]["fields"]["summary"] for x in c.xray.calls_of("CreateTest")] == ["A"]
        assert "skippedDuplicates" not in r and r["updated"] == []
    c, r = run(existingTests="skip")
    assert writes(c) == [] and r["tests"] == []


def test_name_match_with_nothing_to_change_is_only_mapped(tmp_path):
    ctx = ctx_for(tmp_path, _named_world(gherkin="Given a"))
    path = feature_file(tmp_path, NAMED)
    res = preview(ctx, path, **ans(createTestSet=False, aiLabel="ai"))
    assert qids(res) == [] and res["actions"][0]["kind"] == "keep_test"
    res, err = _apply(ctx, path, aiLabel="ai")
    assert not err, res
    assert writes(ctx) == [] and res["unchanged"] == ["P-1"]


def test_name_match_of_a_non_cucumber_test_stays_a_possible_duplicate(tmp_path):
    ctx = ctx_for(tmp_path, _named_world(test_type="Manual"))
    path = feature_file(tmp_path, NAMED)
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == [] and [d["index"] for d in res["possibleDuplicates"]] == [0]


def test_create_is_refused_for_a_test_named_by_a_key_tag(tmp_path):
    ctx = ctx_for(tmp_path, _two_world())
    path = feature_file(tmp_path, TWO_EXISTING)
    res, err = _apply(ctx, path, existingTestOverrides={"P-6": "create"})
    assert err and "only applies to tests matched by name" in res["error"]
    assert writes(ctx) == []
    # a general "create" leaves key-tagged tests unanswered: asked, nothing written
    res, err = _apply(ctx, path, existingTests="create")
    assert not err and res["status"] == "needs_answer" and writes(ctx) == []
    assert res["questions"][-1]["choices"] == ["update", "keep", "skip"]


def test_team_value_on_an_existing_test_is_asked_with_jira_then_written_when_empty(tmp_path):
    world = FakeXrayWorld(existing=_existing_with("customfield_50"))
    world.existing["P-5"]["gherkin"] = "Given x"
    ctx = ctx_for(tmp_path, world)
    ctx.jira = TeamMetaJira()
    ctx.channel = NoFormChannel()
    _team_rules(ctx, {"tag": "team", "field": {"customfield_50": "{value}"}})
    path = feature_file(tmp_path, "Feature: F\n  @TEST_P-5 @team=checkout\n  Scenario: A\n"
                                  "    Given x\n")
    res = preview(ctx, path, **ans(createTestSet=False))
    assert qids(res) == ["tagValue.customfield_50.checkout"]
    res, err = _apply(ctx, path, existingTests="update")
    assert not err and res["status"] == "needs_answer" and ctx.jira.edits == []
    res, err = _apply(ctx, path, existingTests="update",
                      tagValueAnswers={"customfield_50": {"checkout": "t-5"}})
    assert not err, res
    assert ctx.jira.edits == [("P-5", {"customfield_50": "t-5"})]


def test_apply_with_open_choices_lists_value_questions_in_the_same_refusal(tmp_path):
    ctx = ctx_for(tmp_path, yaml=SETTINGS_TEAM)
    path = feature_file(tmp_path, "Feature: F\n  @owner=alice\n  Scenario: A\n    Given a\n")
    res, err = call(ctx, "create_tests_from_feature", projectKey="P", featureFilePath=path,
                    apply=True)
    assert err and res["error"].startswith("Answer these first")
    assert "tagValue.Owner.alice" in qids(res) and ctx.xray.calls == []
