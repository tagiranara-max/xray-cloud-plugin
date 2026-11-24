"""Result-import matching: which test each result case goes to (resultmatch)."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from conftest import make_context
from xray_cloud import resultmatch as rm
from xray_cloud.errors import ToolError


# ---- Cucumber / Behave scenario cases -------------------------------------------------------

def el(name, tags=(), keyword="Scenario", **kw):
    return dict({"keyword": keyword, "name": name, "type": "scenario",
                 "tags": [{"name": t} for t in tags], "steps": []}, **kw)


def report(*elements, feature="F"):
    return [{"name": feature, "elements": list(elements)}]


def test_scenario_cases_keys_from_test_tags_and_key_tag():
    cases = rm.scenario_cases(report(
        el("a", ["@smoke", "@TEST_PROJ-1"]), el("b", ["@issue=PROJ-2"]),
        el("c", ["@issue:PROJ-3"]), el("d", ["@issue_PROJ-4"]), el("e", ["@PROJ-5"]),
        el("f", ["@TEST_PROJ-1", "@TEST_PROJ-6"]), el("g", ["@TEST_PROJ-7",
                                                                  "@issue=PROJ-7"])),
        "cucumber", "PROJ", "issue")
    assert [(c.name, c.tag_keys) for c in cases] == [
        ("a", ["PROJ-1"]), ("b", ["PROJ-2"]), ("c", ["PROJ-3"]), ("d", ["PROJ-4"]),
        ("e", []), ("f", ["PROJ-1", "PROJ-6"]), ("g", ["PROJ-7"])]


def test_scenario_cases_without_key_tag_ignore_custom_tags():
    cases = rm.scenario_cases(report(el("b", ["@issue=PROJ-2"])), "cucumber", "PROJ", None)
    assert cases[0].tag_keys == []


def test_scenario_cases_refuse_keys_of_other_projects():
    with pytest.raises(ToolError, match="OTHER-1"):
        rm.scenario_cases(report(el("a", ["@TEST_OTHER-1"])), "cucumber", "PROJ", None)
    with pytest.raises(ToolError, match="OTHER-2"):
        rm.scenario_cases(report(el("a", ["@issue=OTHER-2"])), "cucumber", "PROJ", "issue")


def test_scenario_cases_skip_backgrounds_and_name_unnamed_and_outline_rows():
    bg = {"keyword": "Background", "type": "background", "steps": []}
    cases = rm.scenario_cases(report(
        bg, el("  Login works  "), el("", line=12),
        el("Outline", keyword="Scenario Outline"), el("Outline", keyword="Scenario Outline"),
        feature="Accounts"), "cucumber", "PROJ", None)
    assert [c.name for c in cases] == ["Login works", "Accounts (line 12)", "Outline", "Outline"]
    assert cases[1].background == [] and cases[0].background  # background carried


def test_behave_tags_are_plain_strings_and_outline_rows_named_after_the_outline():
    rep = [{"name": "F", "elements": [
        {"keyword": "Scenario", "name": "a", "tags": ["TEST_PROJ-1", "smoke"], "steps": []},
        {"keyword": "Scenario Outline", "name": "Login -- @1.1 Users", "tags": [], "steps": []},
        {"keyword": "Scenario Outline", "name": "Login -- @1.2 ", "tags": [], "steps": []}]}]
    cases = rm.scenario_cases(rep, "behave", "PROJ", None)
    assert [(c.name, c.tag_keys, c.outline) for c in cases] == [
        ("a", ["PROJ-1"], False), ("Login", [], True), ("Login", [], True)]


def test_scenario_cases_refuse_non_list_reports():
    with pytest.raises(ToolError, match="list of features"):
        rm.scenario_cases({}, "cucumber", "PROJ", None)


# ---- XML cases (rules verified live, see docs/coverage.md) ----------------------------------

JUNIT = """<?xml version="1.0"?><testsuites><testsuite name="s">
<testcase classname="a.B" name="c"/>
<testcase classname="a.B" name="keyed"><properties><property name="test_key" value="PROJ-3"/>
</properties></testcase>
<testcase name="bare"/>
</testsuite></testsuites>"""


def xml_cases(fmt, text):
    return rm.xml_cases(ET.fromstring(text.encode()), fmt, "PROJ")


def test_junit_definition_is_classname_dot_name_and_test_key_property():
    cases = xml_cases("junit", JUNIT)
    assert [(c.name, c.summary, c.report_keys) for c in cases] == [
        ("a.B.c", "c", []), ("a.B.keyed", "keyed", ["PROJ-3"]), ("bare", "bare", [])]


def test_testng_definition_is_class_dot_method_and_test_attribute():
    cases = xml_cases("testng", """<testng-results><suite><test><class name="a.NG">
      <test-method name="setUp" is-config="true"/>
      <test-method name="m1"/>
      <test-method name="m2"><attributes><attribute name="test"><![CDATA[ PROJ-4 ]]>
      </attribute></attributes></test-method></class></test></suite></testng-results>""")
    assert [(c.name, c.summary, c.report_keys) for c in cases] == [
        ("a.NG.m1", "m1", []), ("a.NG.m2", "m2", ["PROJ-4"])]


def test_nunit_definition_is_fullname_and_test_property():
    cases = xml_cases("nunit", """<test-run><test-suite type="TestFixture" fullname="a.Fx">
      <test-case name="c1" fullname="a.Fx.c1"/>
      <test-case name="c2" fullname="a.Fx.c2"><properties><property name="Test"
        value="PROJ-5"/></properties></test-case>
      <test-case name="a.Fx.old"/></test-suite></test-run>""")
    assert [(c.name, c.summary, c.report_keys) for c in cases] == [
        ("a.Fx.c1", "c1", []), ("a.Fx.c2", "c2", ["PROJ-5"]), ("a.Fx.old", "a.Fx.old", [])]


def test_robot_definition_is_suite_chain_dot_test_and_key_tags():
    cases = xml_cases("robot", """<robot><suite name="Top"><suite name="Sub one">
      <test name="T1"><tags><tag>smoke</tag></tags></test>
      <test name="T2"><tags><tag>PROJ-6</tag></tags></test></suite>
      <test name="T3"/></suite></robot>""")
    assert [(c.name, c.summary, c.report_keys) for c in cases] == [
        ("Top.Sub one.T1", "T1", []), ("Top.Sub one.T2", "T2", ["PROJ-6"]),
        ("Top.T3", "T3", [])]


def test_xml_report_keys_of_other_projects_refused():
    with pytest.raises(ToolError, match="OTHER-1"):
        xml_cases("junit", '<testsuite><testcase name="x"><properties><property name="test_key" '
                           'value="OTHER-1"/></properties></testcase></testsuite>')


def test_parse_xml_refuses_invalid_xml():
    with pytest.raises(ToolError, match="not valid XML"):
        rm.parse_xml(b"<a>", "junit")


# ---- lookups --------------------------------------------------------------------------------

def rows_response(rows, total=None):
    return {"getTests": {"total": len(rows) if total is None else total, "results": [
        {"issueId": str(i), "unstructured": d, "jira": {"key": k, "summary": s}}
        for i, (k, s, d) in enumerate(rows)]}}


def test_find_by_summary_exact_trimmed_case_sensitive_and_batched():
    ctx = make_context(responses={"ResultMatchSummaries": lambda v: rows_response([
        ("PROJ-1", "Login works", None), ("PROJ-2", " Login works ", None),
        ("PROJ-3", "login works", None), ("PROJ-4", "Login works again", None),
        ("OTHER-1", "Login works", None), ("PROJ-5", "Logout", None)])})
    names = ["Login works", "Logout", "Nothing"] + ["n%d" % i for i in range(12)]
    found = rm.find_by_summary(ctx, "PROJ", names)
    assert found["Login works"] == ["PROJ-1", "PROJ-2"]
    assert found["Logout"] == ["PROJ-5"] and found["Nothing"] == []
    calls = ctx.xray.calls_of("ResultMatchSummaries")
    assert len(calls) == 2  # 15 names, 10 per query
    assert calls[0]["jql"].startswith('project = PROJ AND (summary ~ "Login works" OR ')


def test_find_by_summary_pages_until_total():
    pages = [rows_response([("PROJ-%d" % i, "x", None) for i in range(1, 101)], total=101),
             rows_response([("PROJ-200", "Login", None)], total=101)]
    ctx = make_context(responses={"ResultMatchSummaries": lambda v: pages[v["start"] // 100]})
    assert rm.find_by_summary(ctx, "PROJ", ["Login"]) == {"Login": ["PROJ-200"]}


def test_find_by_summary_unsearchable_name_is_missing_without_a_query():
    ctx = make_context(responses={})
    assert rm.find_by_summary(ctx, "PROJ", ["***"]) == {"***": []}


def test_find_by_definition_scans_generic_tests():
    ctx = make_context(responses={"ResultMatchDefinitions": lambda v: rows_response([
        ("PROJ-1", "c", "a.B.c"), ("PROJ-2", "x", "a.B.c"), ("PROJ-3", "y", "a.B.d")])})
    found, complete = rm.find_by_definition(ctx, "PROJ", ["a.B.c", "a.B.d", "z"])
    assert complete and found == {"a.B.c": ["PROJ-1", "PROJ-2"], "a.B.d": ["PROJ-3"],
                                  "z": []}
    v = ctx.xray.calls_of("ResultMatchDefinitions")[0]
    assert v["jql"] == "project = PROJ" and v["testType"] == {"name": "Generic"}


def test_find_by_definition_reports_an_incomplete_scan(monkeypatch):
    monkeypatch.setattr(rm, "MAX_DEFINITION_PAGES", 1)
    ctx = make_context(responses={"ResultMatchDefinitions": lambda v: rows_response(
        [("PROJ-%d" % i, "x", "d%d" % i) for i in range(1, 101)], total=500)})
    found, complete = rm.find_by_definition(ctx, "PROJ", ["d1", "zz"])
    assert not complete and found == {"d1": ["PROJ-1"], "zz": []}


def test_existing_tests_filters_non_tests():
    ctx = make_context(responses={"ResultMatchKeys": rows_response([
        ("PROJ-1", "a", None)])})
    assert rm.existing_tests(ctx, ["PROJ-1", "PROJ-2"]) == {"PROJ-1"}
    assert ctx.xray.calls_of("ResultMatchKeys")[0]["jql"] == "issue in (PROJ-1, PROJ-2)"
