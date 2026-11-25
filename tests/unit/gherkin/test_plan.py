from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import make_context
import xray_cloud.tools  # noqa: F401  (registers tools; plan imports tools.tests lazily)
from xray_cloud.errors import ToolError
from xray_cloud.gherkin.parse import parse_feature
from xray_cloud.gherkin.plan import classify_tag, path_label, plan_import

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "gherkin" / "regression"
YAML = "projects:\n  P: {}\n"


def empty_reads(**over):
    r = {"SearchTests": {"getTests": {"total": 0, "results": []}},
         "GetPreconditions": {"getPreconditions": {"results": []}},
         "FindTestSets": {"getTestSets": {"results": []}},
         "FindCoverableIssues": {"getCoverableIssues": {"results": []}}}
    r.update(over)
    return r


def kinds(plan):
    return [a.kind for a in plan]


def load(name):
    return parse_feature((FIXTURES / name).read_text(encoding="utf-8"), name)


def test_plan_for_sample_feature():
    ctx = make_context(YAML, responses=empty_reads())
    plan = plan_import(ctx, "P", load("sample.feature"), "features/sample.feature", True)
    assert kinds(plan) == ["create_test", "create_test", "create_precondition", "link",
                           "create_test_set"]
    pre = plan[2]
    assert pre.labels == ["features/sample.feature"] and pre.unnamed and pre.tests == [0, 1]
    assert pre.definition == "Given the app is running"
    assert plan[4].summary == "Login" and plan[4].tests == [0, 1]
    assert ctx.xray.calls_of("SearchTests")  # duplicate reads only; nothing written
    assert all(op.startswith(("Search", "Get", "Find")) for op, _ in ctx.xray.calls)


def test_outline_test_definition_contains_all_examples():
    ctx = make_context(YAML, responses=empty_reads())
    plan = plan_import(ctx, "P", load("examples.feature"), "examples.feature", False)
    feat = load("examples.feature")
    outline = next(i for i, s in enumerate(feat.scenarios) if len(s.examples) > 1)
    d = plan[outline].definition
    for ex in feat.scenarios[outline].examples:
        assert ex.keyword + ":" in d
        for row in ex.rows:
            assert all(c in d for c in row)


def test_rule_background_only_linked_to_rule_scenarios():
    ctx = make_context(YAML, responses=empty_reads())
    feat = load("rules.feature")
    plan = plan_import(ctx, "P", feat, "rules.feature", False)
    pres = [a for a in plan if a.kind == "create_precondition"]
    assert [p.summary for p in pres] == ["Feature background", "Rule background"]
    assert pres[0].tests == [0, 1, 2, 3]  # feature background: every scenario
    assert pres[1].tests == [1, 2]        # rule background: only the first rule's scenarios
    # rule and feature tags inherited as labels
    assert plan[1].labels == ["feature", "rule-tag", "scenario-tag"]
    assert plan[3].labels == ["feature"]


def coverable(*keys):
    """getCoverableIssues stand-in: the given keys are requirements Xray knows."""
    def fn(v):
        return {"getCoverableIssues": {"results": [
            {"issueId": "9", "jira": {"key": k, "issuetype": {"name": "Story"}}}
            for k in keys if re.search(r"\b%s\b" % k, v["jql"])]}}
    return fn


def test_tags_become_labels_except_issue_keys():
    text = """@smoke @REQ_Q-9 @R-1
Feature: F
  @fast @TEST_P-5 @REQ_Q-10
  Scenario: A
    Given a
  @slow
  Scenario: B
    Given b
"""
    test5 = {"getTests": {"total": 1, "results": [{"issueId": "55", "testType": {"name": "Cucumber"},
                                                  "jira": {"key": "P-5", "summary": "A", "labels": ["old"]}}]}}

    def search(v):
        return test5 if "P-5" in v["jql"] else {"getTests": {"total": 0, "results": []}}
    ctx = make_context(YAML, responses=empty_reads(
        SearchTests=search, FindCoverableIssues=coverable("R-1", "Q-9", "Q-10")))
    plan = plan_import(ctx, "P", parse_feature(text, "f"), "f.feature", True)
    a, b = plan[0], plan[1]
    assert a.kind == "update_test" and a.key == "P-5" and a.issue_id == "55"
    assert a.labels == ["smoke", "fast"] and a.current_labels == ["old"]
    assert a.requirement_keys == ["Q-9", "R-1", "Q-10"]
    assert b.kind == "create_test" and b.labels == ["smoke", "slow"]
    assert plan[-1].labels == ["smoke"]  # test set: feature tags without keys
    assert [x.key for x in plan if x.kind == "link"] == ["Q-9", "R-1", "Q-10"]


def test_classify_tag_shapes():
    assert classify_tag("@P-5") == ("key", "P-5")
    assert classify_tag("@TEST_smoke") == ("label", "TEST_smoke")
    assert classify_tag("@TEST_p-5") == ("invalid", "TEST_p-5")


KEYS_TEXT = """@ISO-1 @R-1
Feature: F
  @P-5
  Scenario: Mine
    Given a
  @Q-5
  Scenario: Elsewhere
    Given b
  @R-2
  Scenario: Story
    Given c
  @ISO-8601
  Scenario: Not an issue
    Given d
  @TEST_Q-6
  Scenario: Explicit elsewhere
    Given e
  @TEST_P-404
  Scenario: Explicit missing
    Given f
  @REQ_R-404
  Scenario: Missing requirement
    Given g
"""


def _xray_tests(*keys):
    """getTests by key: the given keys are Cucumber tests; a JQL naming ISO-* fails like Jira
    does for a key of no project."""
    def search(v):
        jql = v["jql"]
        if not jql.startswith("issue"):
            return {"getTests": {"total": 0, "results": []}}
        iso = re.findall(r"ISO-\d+", jql)
        if iso:
            raise ToolError("Xray GraphQL error: The issue key '%s' does not exist" % iso[0])
        rows = [{"issueId": k.split("-")[1], "testType": {"name": "Cucumber"},
                 "jira": {"key": k, "summary": k, "labels": []}} for k in keys
                if re.search(r"\b%s\b" % k, jql)]
        return {"getTests": {"total": len(rows), "results": rows}}
    return search


def test_key_tags_are_looked_up_in_xray_and_never_refuse_the_file():
    ctx = make_context(YAML, responses=empty_reads(
        SearchTests=_xray_tests("P-5", "Q-5", "Q-6"), FindCoverableIssues=coverable("R-1", "R-2")))
    plan = plan_import(ctx, "P", parse_feature(KEYS_TEXT, "f"), "f.feature", False)
    by = {a.summary: a for a in plan if a.index is not None}
    assert by["Mine"].kind == "update_test" and by["Mine"].key == "P-5"
    assert by["Elsewhere"].kind == "create_test" and by["Elsewhere"].labels == []
    assert by["Elsewhere"].requirement_keys == ["R-1"]
    assert "Q-5 is a test of project Q" in by["Elsewhere"].not_done[0]["reason"]
    assert by["Story"].requirement_keys == ["R-1", "R-2"]
    assert by["Not an issue"].kind == "create_test"
    # "no such issue" answers are asked about like no rows, never a silent label
    assert by["Not an issue"].labels == []
    assert by["Explicit elsewhere"].kind == "skip_test"
    assert "Q-6 is a test of project Q" in by["Explicit elsewhere"].reason
    assert by["Explicit missing"].kind == "skip_test"
    assert "Xray doesn't know P-404 as a test" in by["Explicit missing"].reason
    # @REQ_ of a key Xray cannot place: linked as the tag asks, but unverified
    assert by["Missing requirement"].requirement_keys == ["R-1", "R-404"]
    assert by["Missing requirement"].unverified_links == ["R-404"]
    jqls = [v["jql"] for op, v in ctx.xray.calls if op == "SearchTests"]
    assert jqls[0] == "issue in (ISO-1, R-1, P-5, Q-5, R-2, ISO-8601, Q-6, P-404, R-404)"
    assert "issue = ISO-8601" in jqls and "issue = Q-6" in jqls
    assert [v["jql"] for op, v in ctx.xray.calls if op == "FindCoverableIssues"] == [
        "issue in (R-1, R-2, P-404, R-404)"]


def test_key_xray_cannot_place_is_asked_and_follows_the_answer():
    text = "@R-9\nFeature: F\n  @ABC-1\n  Scenario: A\n    Given a\n"
    feat = parse_feature(text, "f")
    from xray_cloud.gherkin.plan import KeyInfo
    info = {"R-9": KeyInfo("unknown"), "ABC-1": KeyInfo("unknown")}
    a = analyze_tags(feat, "P", ImportOptions(key_info=info))
    assert a.key_questions == {"R-9": [{"level": "feature", "name": "F"}],
                               "ABC-1": [{"level": "scenario", "name": "A"}]}
    assert a.scenarios[0].outcome.labels == [] and a.scenarios[0].requirement_keys == []
    assert a.any_label  # a "label" answer is possible: tagsAsLabels is asked in the same round
    for ans, labels, reqs in (("label", ["R-9", "ABC-1"], []), ("ignore", [], []),
                              ("link", [], ["R-9", "ABC-1"])):
        a = analyze_tags(feat, "P", ImportOptions(
            key_info=info, key_answers={"R-9": ans, "ABC-1": ans}))
        assert a.key_questions == {}
        st = a.scenarios[0]
        assert (st.outcome.labels, st.requirement_keys) == (labels, reqs), ans
        assert st.unverified == reqs


def test_key_tag_analysis_reports_meanings():
    from xray_cloud.gherkin.plan import KeyInfo
    feat = parse_feature(KEYS_TEXT, "f")
    info = {"P-5": KeyInfo("test"), "Q-5": KeyInfo("test"), "Q-6": KeyInfo("test"),
            "R-1": KeyInfo("issue", "Story"), "R-2": KeyInfo("issue", "Story"),
            "ISO-1": KeyInfo("missing"), "ISO-8601": KeyInfo("missing"),
            "P-404": KeyInfo("missing"), "R-404": KeyInfo("missing")}
    a = analyze_tags(feat, "P", ImportOptions(key_info=info))
    meanings = {k["tag"]: k["meaning"] for k in a.key_tags}
    assert meanings == {"ISO-1": "asked", "R-1": "requirement", "P-5": "test",
                        "Q-5": "otherTest", "R-2": "requirement", "ISO-8601": "asked",
                        "TEST_Q-6": "notImported", "TEST_P-404": "notImported",
                        "REQ_R-404": "missingRequirement"}
    assert list(a.key_questions) == ["ISO-1", "ISO-8601"] and a.any_label
    assert [st.blocked != "" for st in a.scenarios] == [False] * 4 + [True, True, False]


def test_key_tagged_test_must_be_cucumber():
    manual = {"getTests": {"total": 1, "results": [{"issueId": "5", "testType": {"name": "Manual"},
                                                   "jira": {"key": "P-5"}}]}}
    ctx = make_context(YAML, responses=empty_reads(SearchTests=manual))
    feat = parse_feature("Feature: F\n  @TEST_P-5\n  Scenario: A\n    Given a\n", "f")
    plan = plan_import(ctx, "P", feat, "f", False)
    assert plan[0].kind == "skip_test" and "P-5 is a Manual test" in plan[0].reason
    assert plan[0].not_done[0]["what"] == "scenario not imported"


@pytest.mark.parametrize("error", [
    ToolError("Xray API error: HTTP 500", status=500),
    ToolError("Xray rate limit: HTTP 429", status=429),
    ToolError("Xray authentication failed", status=401),
    ToolError("Network error contacting xray.cloud.getxray.app (URLError)")])
def test_key_lookup_failures_stop_before_anything_is_planned(error):
    """A failed read is never taken as "not a test": that would create a duplicate."""
    def search(v):
        raise error
    for text in ("Feature: F\n  @P-5\n  Scenario: A\n    Given a\n",
                 "Feature: F\n  @P-5 @Q-1\n  Scenario: A\n    Given a\n"):
        ctx = make_context(YAML, responses=empty_reads(SearchTests=search))
        with pytest.raises(ToolError) as e:
            plan_import(ctx, "P", parse_feature(text, "f"), "f", False)
        assert e.value is error
        assert [op for op, _ in ctx.xray.calls] == ["SearchTests"]


def test_no_such_issue_refusal_is_retried_key_by_key_even_for_one_key():
    calls = []

    def search(v):
        if not v["jql"].startswith("issue"):
            return {"getTests": {"total": 0, "results": []}}
        calls.append(v["jql"])
        raise ToolError("Xray GraphQL error: The issue key 'ISO-8601' does not exist")
    ctx = make_context(YAML, responses=empty_reads(SearchTests=search))
    feat = parse_feature("Feature: F\n  @ISO-8601\n  Scenario: A\n    Given a\n", "f")
    plan = plan_import(ctx, "P", feat, "f", False)
    assert plan[0].kind == "create_test" and plan[0].labels == []  # asked (keyTag)
    assert calls[:2] == ["issue = ISO-8601", "issue = ISO-8601"]


def test_requirements_xray_knows_are_linked_without_asking():
    ctx = make_context(YAML, responses=empty_reads(FindCoverableIssues=coverable("R-1")))
    feat = parse_feature("@R-1\nFeature: F\n  @REQ_R-2 @R-3\n  Scenario: A\n    Given a\n", "f")
    plan = plan_import(ctx, "P", feat, "f", False)
    assert plan[0].requirement_keys == ["R-1", "R-2"] and plan[0].labels == []
    assert plan[0].unverified_links == ["R-2"]  # R-3 is asked about (keyTag.R-3)
    assert [v["jql"] for op, v in ctx.xray.calls if op == "FindCoverableIssues"] == [
        "issue in (R-1, R-2, R-3)"]


def test_unusable_tags_leave_one_scenario_out_never_the_file():
    text = ("@TEST_P-9 @PRECOND_P-8\nFeature: F\n"
            "  @TEST_P-5 @P-6\n  Scenario: Two keys\n    Given a\n"
            "  @TEST_P-5\n  Scenario: First\n    Given b\n"
            "  @P-5\n  Scenario: Second\n    Given c\n"
            "  @TEST_p-1 @PRECOND_P-8\n  Scenario: Odd tags\n    Given d\n")
    from xray_cloud.gherkin.plan import KeyInfo
    info = {k: KeyInfo("test") for k in ("P-5", "P-6", "P-9")}
    a = analyze_tags(parse_feature(text, "f"), "P", ImportOptions(key_info=info))
    assert [bool(st.blocked) for st in a.scenarios] == [True, False, True, False]
    assert [st.test_key for st in a.scenarios] == [None, "P-5", None, None]
    assert "more than one test key tag" in a.scenarios[0].blocked
    assert "already named by scenario 'First'" in a.scenarios[2].blocked
    assert [n["what"] for n in a.notes] == ["tag @TEST_P-9 (ignored)", "tag @PRECOND_P-8 (ignored)"]
    assert [n["what"] for n in a.scenarios[3].notes] == ["tag @TEST_p-1",
                                                         "tag @PRECOND_P-8 (ignored)"]


def _pre(key, summary, labels, definition, issue_id="9", ptype="Cucumber"):
    return {"issueId": issue_id, "preconditionType": {"name": ptype}, "definition": definition,
            "jira": {"key": key, "summary": summary, "labels": labels}}


def test_precondition_reused_by_key_comment():
    text = "Feature: F\n  Background:\n    #@PRECOND_P-7\n    Given x\n  Scenario: A\n    Given a\n"
    ctx = make_context(YAML, responses=empty_reads(GetPreconditions=lambda v: {
        "getPreconditions": {"results": [_pre("P-7", "Old", [], "Given x")]
                             if v["jql"] == "issue = P-7" else []}}))
    plan = plan_import(ctx, "P", parse_feature(text, "f"), "f.feature", False)
    pre = plan[1]
    assert pre.kind == "reuse_precondition" and pre.key == "P-7" and not pre.update_definition
    # a Pre-Condition of another project is ignored with a note, never a refusal
    plan = plan_import(ctx, "P", parse_feature(text.replace("P-7", "Q-7"), "f"), "f", False)
    assert plan[1].kind == "create_precondition"
    from xray_cloud.gherkin.plan import analyze_tags as _an
    notes = _an(parse_feature(text.replace("P-7", "Q-7"), "f"), "P").notes
    assert notes[0]["what"] == "#@PRECOND_Q-7 (ignored)"


def test_precondition_reused_by_path_label_then_summary():
    text = """Feature: F
  Background: Setup
    Given x
  Scenario: A
    Given a
  Rule: R
    Background:
      Given y
    Scenario: B
      Given b
"""
    label_hits = [_pre("P-1", "Background for: P-90", ["dir/f.feature"], "Given y", "1"),
                  _pre("P-2", "Other", ["dir/f.feature"], "Given z", "2")]
    summary_hits = [_pre("P-3", "Setup", [], "Given old", "3"),
                    _pre("P-4", "Setup extra", [], "Given x", "4")]

    def pre(v):
        if "labels =" in v["jql"]:
            return {"getPreconditions": {"results": label_hits}}
        if "summary ~" in v["jql"]:
            return {"getPreconditions": {"results": summary_hits}}
        return {"getPreconditions": {"results": []}}
    ctx = make_context(YAML, responses=empty_reads(GetPreconditions=pre))
    plan = plan_import(ctx, "P", parse_feature(text, "f"), "dir/f.feature", False)
    setup, unnamed = [a for a in plan if "precondition" in a.kind]
    # named: not found by label (no label summary "Setup") -> exact summary match P-3
    assert setup.kind == "reuse_precondition" and setup.key == "P-3" and setup.update_definition
    # unnamed: label + "Background for:" + same definition -> P-1
    assert unnamed.kind == "reuse_precondition" and unnamed.key == "P-1"
    assert not unnamed.update_definition and unnamed.unnamed
    jqls = [v["jql"] for v in ctx.xray.calls_of("GetPreconditions")]
    assert jqls[0] == 'project = P AND labels = "dir/f.feature"'
    # label match wins over summary: a labelled "Setup" is preferred
    label_hits.append(_pre("P-8", "Setup", ["dir/f.feature"], "Given x", "8"))
    ctx = make_context(YAML, responses=empty_reads(GetPreconditions=pre))
    plan = plan_import(ctx, "P", parse_feature(text, "f"), "dir/f.feature", False)
    assert [a.key for a in plan if "precondition" in a.kind] == ["P-8", "P-1"]


def test_test_set_reused_by_exact_summary():
    ctx = make_context(YAML, responses=empty_reads(FindTestSets={"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "Login extra"}},
        {"issueId": "71", "jira": {"key": "P-71", "summary": "Login"}}]}}))
    plan = plan_import(ctx, "P", load("sample.feature"), "s", True)
    assert plan[-1].kind == "reuse_test_set" and plan[-1].key == "P-71"


def test_path_label_has_no_spaces():
    assert path_label("my dir/a b.feature") == "my_dir/a_b.feature"


TWO_RULES = """Feature: F
  @a
  Rule: %s
    Scenario: One
      Given one
  @b %s
  Rule: %s
    Scenario: Two
      Given two
"""


@pytest.mark.parametrize("first,second", [("", ""), ("Same", "Same")])
def test_rule_tags_classified_per_rule_even_when_names_repeat(first, second):
    ctx = make_context(YAML, responses=empty_reads())
    feat = parse_feature(TWO_RULES % (first, "@c", second), "f")
    plan = plan_import(ctx, "P", feat, "f", False)
    assert plan[0].labels == ["a"] and plan[1].labels == ["b", "c"]
    feat = parse_feature(TWO_RULES % (first, "@TEST_Q-1", second), "f")
    a = analyze_tags(feat, "P", ImportOptions(key_info={}))
    assert a.notes[0]["what"] == "tag @TEST_Q-1 (ignored)"  # ignored, never a refusal


# -- import choices: inline Backgrounds, tag rules (spec 6.2, 6.4, 8) -------------------------

import textwrap  # noqa: E402

from gherkin_helpers import steps_of  # noqa: E402
from xray_cloud.config import FieldSpec, ProjectSettings  # noqa: E402
from xray_cloud.gherkin.plan import ImportOptions, analyze_tags  # noqa: E402
from xray_cloud.gherkin.tagrules import parse_rules  # noqa: E402

FIELDS = {"Component": FieldSpec("Component", "customfield_9", None, "option", False, None)}
PROJECT = ProjectSettings("P", fields={"test": FIELDS})
RULES = parse_rules([{"tag": "suite", "label": "{value}"},
                     {"tag": ["area", "areas"], "split": ",", "testSet": "{value}"},
                     {"tag": "comp", "field": {"Component": "{value}"}}],
                    set(FIELDS), "tagRules")


def reparse(definition):
    doc = "Feature: RT\n\n  Scenario: RT\n" + textwrap.indent(definition, "    ") + "\n"
    return parse_feature(doc, "rt").scenarios[0]


def test_inline_backgrounds_keep_tables_and_doc_strings():
    """Review Focus 5: feature + rule Background steps, then the scenario's, read back intact."""
    feat = load("inline_backgrounds.feature")
    ctx = make_context(YAML, responses=empty_reads())
    plan = plan_import(ctx, "P", feat, "f.feature",
                       ImportOptions(create_test_set=False, backgrounds="inline"))
    assert kinds(plan) == ["create_test", "create_test"]  # no Pre-Conditions, no links
    assert not ctx.xray.calls_of("GetPreconditions")
    top, billing = feat.scenarios
    assert [b.scope for b in billing.backgrounds] == ["feature", "rule"]
    for action, s in zip(plan, feat.scenarios):
        again = reparse(action.definition)
        expected = [st for bg in s.backgrounds for st in bg.steps] + s.steps
        assert steps_of(again.steps) == steps_of(expected)
        assert [(e.header, e.rows) for e in again.examples] == [(e.header, e.rows)
                                                               for e in s.examples]
    rows = reparse(plan[0].definition).steps[0].data_table
    assert rows == [["name", "role"], ["alice", "admin"], ["bob", "a|b"]]
    doc = reparse(plan[1].definition).steps[1].doc_string
    assert doc.content == '{"mode": "strict"}\n\n  indented line' and doc.media_type == "json"
    assert reparse(plan[1].definition).steps[2].doc_string.content == 'Line one\nLine """ two'


def test_rule_outcomes_per_test_inherit_feature_and_rule_tags():
    feat = load("tags_keyvalue.feature")
    ctx = make_context(YAML, responses=empty_reads())
    opts = ImportOptions(create_test_set=True, rules=RULES, project=PROJECT)
    plan = plan_import(ctx, "P", feat, "tags.feature", opts)
    tests = [a for a in plan if a.kind == "create_test"]
    assert [(t.labels, t.test_sets, t.fields) for t in tests] == [
        (["regression", "team"], ["ui"], {}),
        (["regression", "team", "wip"], ["api", "users"], {"Component": "backend"}),
        (["regression", "team"], ["api", "users"], {"Component": "backend"}),
    ]
    sets = [a for a in plan if a.kind == "create_test_set"]
    assert [(s.summary, s.tests, s.labels) for s in sets] == [
        ("Tagged checkout", [0, 1, 2], ["regression", "team"]),  # feature labels on the set
        ("ui", [0], []), ("api", [1, 2], []), ("users", [1, 2], [])]
    # tagsAsLabels=false: unmatched plain tags are not labels; rule actions still apply
    opts.tags_as_labels = False
    plan = plan_import(ctx, "P", feat, "tags.feature", opts)
    tests = [a for a in plan if a.kind == "create_test"]
    assert [t.labels for t in tests] == [["regression"], ["regression"], ["regression"]]
    assert tests[1].test_sets == ["api", "users"] and tests[1].fields == {"Component": "backend"}
    assert [s.labels for s in plan if s.kind == "create_test_set"] == [
        ["regression"], [], [], []]


def test_examples_tags_are_not_used_for_actions():
    text = ("Feature: F\n  Scenario Outline: A\n    Given <x>\n\n    @suite=smoke\n"
            "    Examples:\n      | x |\n      | 1 |\n")
    ctx = make_context(YAML, responses=empty_reads())
    plan = plan_import(ctx, "P", parse_feature(text, "f"), "f",
                       ImportOptions(create_test_set=False, rules=RULES, project=PROJECT))
    assert plan[0].labels == [] and plan[0].test_sets == []


def test_feature_set_and_tag_set_with_the_same_name_are_one_set():
    text = "Feature: ui\n  @area=ui\n  Scenario: A\n    Given a\n  Scenario: B\n    Given b\n"
    ctx = make_context(YAML, responses=empty_reads())
    plan = plan_import(ctx, "P", parse_feature(text, "f"), "f",
                       ImportOptions(create_test_set=True, rules=RULES, project=PROJECT))
    sets = [a for a in plan if "test_set" in a.kind]
    assert [(s.kind, s.summary, s.tests) for s in sets] == [("create_test_set", "ui", [0, 1])]
    assert len(ctx.xray.calls_of("FindTestSets")) == 1


def test_ambiguous_test_set_name_refused_listing_keys():
    sets = {"getTestSets": {"results": [
        {"issueId": "70", "jira": {"key": "P-70", "summary": "ui"}},
        {"issueId": "71", "jira": {"key": "P-71", "summary": "ui"}},
        {"issueId": "72", "jira": {"key": "P-72", "summary": "ui extra"}}]}}
    ctx = make_context(YAML, responses=empty_reads(FindTestSets=sets))
    feat = parse_feature("Feature: F\n  @area=ui\n  Scenario: A\n    Given a\n", "f")
    with pytest.raises(ToolError, match="ambiguous") as e:
        plan_import(ctx, "P", feat, "f",
                    ImportOptions(create_test_set=False, rules=RULES, project=PROJECT))
    assert "P-70" in e.value.message and "P-71" in e.value.message
    assert "P-72" not in e.value.message


def test_tag_rule_problems_refuse_before_any_call():
    bad = parse_rules([{"tag": "suite", "label": "x{value}"}], set(), "tagRules")
    ctx = make_context(YAML, responses=empty_reads())
    feat = parse_feature("Feature: F\n  @suite=a\\tb\n  Scenario: A\n    Given a\n", "f")
    feat.scenarios[0].tags = ["@suite=a b"]  # whitespace cannot be written in a tag; inject it
    with pytest.raises(ToolError, match="invalid") as e:
        plan_import(ctx, "P", feat, "f", ImportOptions(create_test_set=False, rules=bad))
    assert "suite=a b" in e.value.message and "rule 0" in e.value.message
    feat = parse_feature("@comp=a\nFeature: F\n  @comp=b\n  Scenario: A\n    Given a\n", "f")
    with pytest.raises(ToolError, match="conflicting"):
        plan_import(ctx, "P", feat, "f",
                    ImportOptions(create_test_set=False, rules=RULES, project=PROJECT))
    assert ctx.xray.calls == []


def test_feature_tag_problem_reported_once():
    bad = parse_rules([{"tag": "suite", "label": "x{value}"}], set(), "tagRules")
    feat = parse_feature("Feature: F\n  Scenario: A\n    Given a\n  Scenario: B\n    Given b\n", "f")
    feat.tags = ["@suite=a b"]
    with pytest.raises(ToolError) as e:
        analyze_tags(feat, "P", ImportOptions(rules=bad))
    assert len(e.value.data["problems"]) == 1


def test_requirement_tag_naming_a_test_is_not_linked():
    from xray_cloud.gherkin.plan import KeyInfo
    feat = parse_feature("Feature: F\n  @REQ_P-5\n  Scenario: A\n    Given a\n", "f")
    a = analyze_tags(feat, "P", ImportOptions(key_info={"P-5": KeyInfo("test")}))
    st = a.scenarios[0]
    assert st.requirement_keys == [] and st.notes[0]["what"] == "requirement link P-5"
    assert st.notes[0]["reason"] == "P-5 is a test, not a requirement"
