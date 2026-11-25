from __future__ import annotations

import json

import pytest

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = """
projects:
  P:
    fields:
      test:
        Severity: {id: customfield_1, type: option, required: true}
"""
YAML_NOREQ = "projects:\n  P: {}\n"


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def row(key="P-1", summary="Login works", **extra):
    return {"issueId": "100", "testType": {"name": "Manual"},
            "jira": dict({"key": key, "summary": summary, "status": {"name": "To Do"},
                          "labels": ["x"]}, **extra)}


def results(*rows):
    return {"getTests": {"total": len(rows), "results": list(rows)}}


class FakeJira:
    def __init__(self):
        self.updates = []

    def edit_issue(self, key, fields):
        self.updates.append((key, fields))


def test_search_requires_query_or_jql():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "search_tests", projectKey="P")
    assert err and "query or jql" in res["error"]


def test_search_escapes_quotes_in_summary_query():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(row())})
    res, _ = call(ctx, "search_tests", projectKey="P", query='say "hi"')
    jql = ctx.xray.calls_of("SearchTests")[0]["jql"]
    assert jql == 'project = P AND summary ~ "say hi"'
    assert res["tests"] == [{"key": "P-1", "summary": "Login works", "status": "To Do",
                             "testType": "Manual", "labels": ["x"]}]


def test_text_search_strips_lucene_specials_and_control_chars():
    # Verified live: "[" in a summary ~ phrase stops the issue matching its own summary,
    # and a raw TAB makes the JQL invalid.
    from xray_cloud.context import jql_str, jql_text
    assert jql_text('[live] dup [check] (x) "q"') == '"live dup check x q"'
    assert jql_text("Grüße\tmit Tab") == '"Grüße mit Tab"'
    assert jql_text("[]()") is None
    assert jql_str('a\tb\n"c"\\') == '"a\\tb\\n\\"c\\"\\\\"'
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results()})
    res, err = call(ctx, "search_tests", projectKey="P", query="[]")
    assert err and "searchable" in res["error"] and ctx.xray.calls == []


def test_search_by_issue_key_and_jql():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results()})
    call(ctx, "search_tests", projectKey="P", query="P-12")
    call(ctx, "search_tests", projectKey="P", jql="labels = a")
    assert [c["jql"] for c in ctx.xray.calls_of("SearchTests")] == [
        "project = P AND issue = P-12", "project = P AND (labels = a)"]


def test_create_test_returns_duplicates_without_creating():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(row())})
    res, _ = call(ctx, "create_test", projectKey="P", aiLabel="", summary="Login works")
    assert res["status"] == "possible_duplicates"
    assert res["candidates"] == [{"key": "P-1", "summary": "Login works"}]
    assert "allowDuplicates" in res["hint"]
    assert ctx.xray.calls_of("CreateTest") == []


def test_only_exact_summaries_are_duplicates_similar_ones_are_listed():
    from xray_cloud.tools.tests import check_duplicates
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(
        row("P-1", "Devices I signed in from"), row("P-2", " sign  IN "))})
    dups, similar = check_duplicates(ctx, "P", "Sign in")
    assert dups == [{"key": "P-2", "summary": " sign  IN "}]
    assert similar == [{"key": "P-1", "summary": "Devices I signed in from"}]
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(
        row("P-1", "Devices I signed in from"))})
    assert check_duplicates(ctx, "P", "Sign in") == (
        [], [{"key": "P-1", "summary": "Devices I signed in from"}])


def test_create_test_with_only_similar_summaries_creates_and_lists_them():
    ctx = make_context(YAML_NOREQ, responses={
        "SearchTests": results(row("P-1", "Devices I signed in from")),
        "CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="Sign in")
    assert not err and res["key"] == "P-9"
    assert res["similar"] == [{"key": "P-1", "summary": "Devices I signed in from"}]


def test_create_test_exact_match_after_normalising_blocks_and_lists_similar():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(
        row("P-1", "Devices I signed in from"), row("P-2", "SIGN   in"))})
    res, _ = call(ctx, "create_test", projectKey="P", aiLabel="", summary=" Sign in ")
    assert res["status"] == "possible_duplicates"
    assert res["candidates"] == [{"key": "P-2", "summary": "SIGN   in"}]
    assert res["similar"] == [{"key": "P-1", "summary": "Devices I signed in from"}]
    assert ctx.xray.calls_of("CreateTest") == []


def test_duplicate_search_pages_until_exact_and_is_bounded():
    from xray_cloud.errors import ToolError
    from xray_cloud.tools.tests import MAX_DUP_PAGES, check_duplicates

    def pages(v):
        start = v.get("start", 0)
        rows = [row("P-%d" % (start + i + 1), "Sign in to x %d" % (start + i))
                for i in range(100)]
        if start == 200:
            rows[5] = row("P-999", "sign in")
        return results(*rows)
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": pages})
    dups, similar = check_duplicates(ctx, "P", "Sign in")
    assert dups == [{"key": "P-999", "summary": "sign in"}] and len(similar) == 10
    assert [c.get("start", 0) for c in ctx.xray.calls_of("SearchTests")] == [0, 100, 200]
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": lambda v: results(
        *[row("P-%d" % i, "Sign in to %d" % i) for i in range(100)])})
    with pytest.raises(ToolError, match="Cannot tell"):
        check_duplicates(ctx, "P", "Sign in")
    assert len(ctx.xray.calls_of("SearchTests")) == MAX_DUP_PAGES


def test_create_test_duplicate_check_disabled_by_settings():
    ctx = make_context("duplicateCheck: false\nprojects:\n  P: {}\n", responses={
        "CreateTest": {"createTest": {"test": {"issueId": "9", "jira": {"key": "P-9"}}}}})
    res, _ = call(ctx, "create_test", projectKey="P", aiLabel="", summary="Login works")
    assert res["key"] == "P-9"


def create_resp():
    return {"createTest": {"test": {"issueId": "9", "jira": {"key": "P-9"}}, "warnings": []}}


def test_create_test_with_allow_duplicates_creates_with_steps():
    ctx = make_context(YAML_NOREQ, responses={"CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", description="d",
                    allowDuplicates=True, steps=[{"action": "a", "result": "r"}])
    assert not err
    assert res == {"key": "P-9", "summary": "S", "testType": "Manual", "created": True}
    v = ctx.xray.calls_of("CreateTest")[0]
    assert v["jira"] == {"fields": {"project": {"key": "P"}, "summary": "S", "description": "d"}}
    assert v["testType"] == {"name": "Manual"} and "gherkin" not in v
    assert v["steps"] == [{"action": "a", "data": "", "result": "r"}]
    assert ctx.xray.calls_of("SearchTests") == []


def test_create_test_gherkin_defaults_to_cucumber():
    ctx = make_context(YAML_NOREQ, responses={"CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", gherkin="Given x",
                    allowDuplicates=True)
    assert not err and res["testType"] == "Cucumber"
    v = ctx.xray.calls_of("CreateTest")[0]
    assert v["testType"] == {"name": "Cucumber"} and v["gherkin"] == "Given x"
    call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", gherkin="Given x",
         testType="Generic", allowDuplicates=True)
    assert ctx.xray.calls_of("CreateTest")[1]["testType"] == {"name": "Generic"}


def test_create_test_gherkin_with_steps_refused_before_any_call():
    ctx = make_context(YAML_NOREQ, responses={"CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", gherkin="Given x",
                    steps=[{"action": "a", "result": "r"}])
    assert err and "not both" in res["error"] and ctx.xray.calls == []


def test_create_test_maps_fields_and_refuses_missing_required_before_any_call():
    ctx = make_context(YAML, responses={"CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", allowDuplicates=True)
    assert not err and res["status"] == "needs_answer"
    assert res["missingFields"][0]["name"] == "Severity"
    assert ctx.xray.calls == []
    call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", allowDuplicates=True,
         fields={"Severity": "High"})
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["customfield_1"] == {"value": "High"}


def test_create_test_with_folder_partial_failure_reports_key():
    from xray_cloud.errors import ToolError

    def boom(_):
        raise ToolError("boom")
    ctx = make_context(YAML_NOREQ, responses={
        "CreateTest": create_resp(), "CreateFolder": {"createFolder": {}},
        "AddTestsToFolder": boom})
    ctx.project_ids["P"] = "1"
    res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", allowDuplicates=True,
                    folderPath="a/b")
    assert err and res["created"] == ["P-9"] and "P-9" in res["error"]
    assert ctx.xray.calls_of("CreateFolder")[0]["path"] == "/a/b"


def test_create_test_with_folder_success():
    ctx = make_context(YAML_NOREQ, responses={
        "CreateTest": create_resp(), "CreateFolder": {"createFolder": {}},
        "AddTestsToFolder": {"addTestsToFolder": {}}})
    ctx.project_ids["P"] = "1"
    res, _ = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", allowDuplicates=True,
                  folderPath="/a")
    assert res["folderPath"] == "/a"
    assert ctx.xray.calls_of("AddTestsToFolder")[0]["testIssueIds"] == ["9"]


def test_create_test_read_only_refused():
    ctx = make_context(YAML_NOREQ, read_only=True)
    res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S")
    assert err and "read-only" in res["error"]


def test_update_test_type():
    ctx = make_context(YAML_NOREQ, responses={
        "SearchTests": results(row()), "UpdateTestType": {"updateTestType": {}}})
    res, _ = call(ctx, "update_test_type", projectKey="P", testKey="P-1", testType="Generic")
    assert res["testType"] == "Generic"
    assert ctx.xray.calls_of("UpdateTestType")[0] == {"issueId": "100",
                                                      "testType": {"name": "Generic"}}


def test_edit_test_gherkin_uses_xray():
    ctx = make_context(YAML_NOREQ, responses={
        "SearchTests": results(row()), "UpdateGherkin": {"updateGherkinTestDefinition": {}}})
    res, _ = call(ctx, "edit_test", projectKey="P", testKey="P-1", gherkin="Feature: y")
    assert res == {"key": "P-1", "updated": ["gherkin"]}
    assert ctx.xray.calls_of("UpdateGherkin")[0]["gherkin"] == "Feature: y"


from xray_cloud.tools.tests import JIRA_TOKEN_MESSAGE as GUIDANCE  # noqa: E402


def test_edit_test_jira_fields_without_token_returns_guidance():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "edit_test", projectKey="P", testKey="P-1", summary="new")
    assert err and res["error"] == GUIDANCE


def test_edit_test_both_without_token_applies_nothing():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "edit_test", projectKey="P", testKey="P-1", summary="n", gherkin="g")
    assert err and res["error"] == GUIDANCE and ctx.xray.calls == []


def test_edit_test_jira_path_with_token():
    ctx = make_context(YAML, responses={})
    ctx.jira = FakeJira()
    res, _ = call(ctx, "edit_test", projectKey="P", testKey="P-1", summary="n", priority="High",
                  labels=["a"])
    assert ctx.jira.updates == [("P-1", {"summary": "n", "labels": ["a"],
                                         "priority": {"name": "High"}})]
    assert set(res["updated"]) == {"summary", "labels", "priority"}


def test_edit_test_nothing_to_update():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "edit_test", projectKey="P", testKey="P-1")
    assert err


def test_delete_test_without_confirm_previews():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(row())})
    res, _ = call(ctx, "delete_test", projectKey="P", testKey="P-1")
    assert res["wouldDelete"] == {"key": "P-1", "summary": "Login works"}
    assert ctx.xray.calls_of("DeleteTest") == []


def test_delete_test_confirmed():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(row()),
                                              "DeleteTest": {"deleteTest": True}})
    res, _ = call(ctx, "delete_test", projectKey="P", testKey="P-1", confirm=True)
    assert res == {"key": "P-1", "deleted": True}
    assert ctx.xray.calls_of("DeleteTest")[0] == {"issueId": "100"}


def test_wrong_project_key_rejected():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "delete_test", projectKey="P", testKey="Q-1")
    assert err and "does not belong" in res["error"]


def test_get_test_compact_vs_detail():
    r = row(description="desc", priority={"name": "High"})
    r["steps"] = [{"action": "a"}]
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(r)})
    compact_res, _ = call(ctx, "get_test", projectKey="P", testKey="P-1")
    assert set(compact_res) == {"key", "summary", "status", "testType", "labels"}
    detail, _ = call(ctx, "get_test", projectKey="P", testKey="P-1", detail=True)
    assert detail["description"] == "desc" and detail["priority"] == "High"
    assert detail["steps"] == [{"action": "a"}]


def test_get_test_not_found():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results()})
    res, err = call(ctx, "get_test", projectKey="P", testKey="P-1")
    assert err and "not found" in res["error"]


def test_all_tools_schema_shape():
    for name in ("search_tests", "get_test", "create_test", "update_test_type", "edit_test",
                 "delete_test", "set_lifecycle_label"):
        t = REGISTRY.get(name)
        assert t.params["additionalProperties"] is False and "projectKey" in t.params["required"]
        assert t.write == (name not in ("search_tests", "get_test"))


def test_search_issue_key_is_scoped_to_project():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(row("Q-5"))})
    res, _ = call(ctx, "search_tests", projectKey="P", query="Q-5")
    assert ctx.xray.calls_of("SearchTests")[0]["jql"] == "project = P AND issue = Q-5"
    assert res["tests"] == []  # post-filter drops other-project keys


def test_search_rejects_unbalanced_jql():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "search_tests", projectKey="P", jql="labels=a) OR (project = Q")
    assert err and "unbalanced" in res["error"] and ctx.xray.calls == []
    _, err = call(ctx, "search_tests", projectKey="P", jql="(labels=a")
    assert err


def test_search_ignores_parens_inside_quotes():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results()})
    call(ctx, "search_tests", projectKey="P", jql='summary ~ "a)"')
    assert ctx.xray.calls_of("SearchTests")[0]["jql"] == 'project = P AND (summary ~ "a)")'


def test_search_order_by_is_split_and_reappended():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results()})
    call(ctx, "search_tests", projectKey="P", query="x", jql="labels = a ORDER BY created DESC")
    assert ctx.xray.calls_of("SearchTests")[0]["jql"] == (
        'project = P AND summary ~ "x" AND (labels = a) ORDER BY created DESC')
    call(ctx, "search_tests", projectKey="P", jql="order by key")
    assert ctx.xray.calls_of("SearchTests")[1]["jql"] == "project = P order by key"


def test_search_post_filters_other_project_keys():
    ctx = make_context(YAML_NOREQ, responses={"SearchTests": results(row("P-1"), row("Q-2"))})
    res, _ = call(ctx, "search_tests", projectKey="P", query="x")
    assert [t["key"] for t in res["tests"]] == ["P-1"]


def test_create_test_invalid_folder_path_makes_no_api_calls():
    for bad in ("..", "/a/../b", "/", ""):
        ctx = make_context(YAML_NOREQ)
        res, err = call(ctx, "create_test", projectKey="P", aiLabel="", summary="S", folderPath=bad)
        assert err and "created" not in res
        assert ctx.xray.calls == []


def test_build_search_jql_specials_only_query_is_refused():
    from xray_cloud.errors import ToolError
    from xray_cloud.tools.tests import build_search_jql
    with pytest.raises(ToolError) as ei:
        build_search_jql("P", query='[]{}()"')
    assert ei.value.message == "query has no searchable text"
    assert build_search_jql("P", query="a [b]") == 'project = P AND summary ~ "a b"'


# ---- AI label choice (create_test)

def test_create_test_unanswered_ai_label_asks_with_zero_calls():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "create_test", projectKey="P", summary="S", allowDuplicates=True)
    assert not err and res["status"] == "needs_answer"
    assert [q["id"] for q in res["questions"]] == ["aiLabel"]
    assert ctx.xray.calls == []


def test_create_test_ai_label_is_added_remembered_and_not_asked_again():
    ctx = make_context(YAML_NOREQ, responses={"CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", summary="S", allowDuplicates=True,
                    aiLabel="bot", rawFields={"labels": ["x", "bot"]})
    assert not err and res["created"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["x", "bot"]
    res, err = call(ctx, "create_test", projectKey="P", summary="S2", allowDuplicates=True)
    assert not err and res["created"]
    assert ctx.xray.calls_of("CreateTest")[1]["jira"]["fields"]["labels"] == ["bot"]


def test_create_test_empty_ai_label_means_no_label_and_is_remembered():
    ctx = make_context(YAML_NOREQ, responses={"CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", summary="S", allowDuplicates=True,
                    aiLabel="")
    assert not err and res["created"]
    assert "labels" not in ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]
    res, err = call(ctx, "create_test", projectKey="P", summary="S2", allowDuplicates=True)
    assert not err and res["created"]


def test_create_test_settings_ai_label_means_no_question():
    ctx = make_context("aiCreated: {label: from-settings}\nprojects:\n  P: {}\n",
                       responses={"CreateTest": create_resp()})
    res, err = call(ctx, "create_test", projectKey="P", summary="S", allowDuplicates=True)
    assert not err and res["created"]
    assert ctx.xray.calls_of("CreateTest")[0]["jira"]["fields"]["labels"] == ["from-settings"]


def test_create_test_whitespace_ai_label_refused_before_any_call():
    ctx = make_context(YAML_NOREQ)
    res, err = call(ctx, "create_test", projectKey="P", summary="S", aiLabel="two words")
    assert err and "aiLabel" in res["error"]
    assert ctx.xray.calls == []


def test_create_test_differing_ai_label_is_call_only_unless_remember():
    ctx = make_context(YAML_NOREQ, responses={"CreateTest": create_resp()})
    base = dict(projectKey="P", summary="S", allowDuplicates=True)
    call(ctx, "create_test", aiLabel="first", **base)
    call(ctx, "create_test", aiLabel="other", **base)
    call(ctx, "create_test", **base)
    labels = [c["jira"]["fields"]["labels"] for c in ctx.xray.calls_of("CreateTest")]
    assert labels == [["first"], ["other"], ["first"]]
    call(ctx, "create_test", aiLabel="other", remember=True, **base)
    call(ctx, "create_test", **base)
    assert ctx.xray.calls_of("CreateTest")[4]["jira"]["fields"]["labels"] == ["other"]
