"""Live checks for the feature-import choices, tag rules, AI label, analyze_feature_tags and
get_status (XRAY_TEST_PROJECT). Each test gets its own ServerContext (`fresh`) with a temp
settings file and a temp CLAUDE_PLUGIN_DATA, so remembered answers start empty and the user's
real preferences are never touched. Everything a test creates is deleted in its teardown, also
when the test fails.

XRAY_TEST_DATE_FIELD (optional): the Jira field id of a date field on the test project's Test
screen (e.g. customfield_12345). Without it the tag-rule field checks are skipped; the rest of
the tag-rule test still runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

import pytest

from xray_cloud.choices import region_of
from xray_cloud.gherkin.parse import parse_feature
from xray_cloud.registry import REGISTRY

from .conftest import HAS_JIRA, HAS_PROJECT, HAS_XRAY, LIVE_LABEL, RUN_ID, Live
from .test_live_write import REGRESSION, prefixed_copy, reparse

QUESTION_IDS = {"createTestSet", "backgrounds", "tagsAsLabels", "aiLabel", "aiLabelScope"}
# A date field on the test project's Test screen; tenant-specific, so only from the env.
START_FIELD = (os.environ.get("XRAY_TEST_DATE_FIELD") or "").strip() or None
GQL_ISSUE = """
query Q($jql: String!) { %s(jql: $jql, limit: 1) { results { issueId
  jira(fields: [%s]) %s } } }
"""
MEMBERS = 'tests(limit: 50) { results { jira(fields: ["key"]) } }'
SECRET_ENV = ("XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "JIRA_API_TOKEN", "JIRA_EMAIL")


@pytest.fixture
def fresh(tmp_path):
    """A Live context of its own; `settings` (YAML text) may be rewritten before the first
    call. Teardown deletes what it created and fails the test if anything is left."""
    if not (HAS_XRAY and HAS_PROJECT):
        pytest.skip("XRAY_CLIENT_ID/SECRET and XRAY_TEST_PROJECT not set")
    lv = Live(tmp_path)
    assert lv.env["CLAUDE_PLUGIN_DATA"].startswith(str(tmp_path))
    yield lv
    failed = lv.cleanup()
    assert not failed and not lv.denied, "cleanup incomplete: failed=%s denied=%s" % (
        failed, lv.denied)


def settings(lv: Live, text: str) -> None:
    (lv.root / "settings.yml").write_text(text, encoding="utf-8")


def write_feature(lv: Live, name: str, text: str) -> str:
    path = Path(lv.env["CLAUDE_PROJECT_DIR"]) / lv.run_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path.relative_to(lv.env["CLAUDE_PROJECT_DIR"]))


def issue(lv: Live, query: str, key: str, extra: str = "") -> dict:
    names = ["key", "summary", "labels"] + ([START_FIELD] if START_FIELD else [])
    rows = lv.gql(GQL_ISSUE % (query, ", ".join('"%s"' % n for n in names), extra),
                  {"jql": "issue = %s" % key})[query]["results"]
    assert rows, (query, key)
    return rows[0]


def members(lv: Live, set_key: str) -> List[str]:
    row = issue(lv, "getTestSets", set_key, MEMBERS)
    return sorted(r["jira"]["key"] for r in row["tests"]["results"])


# ---------------------------------------------------------------------------------------------

def test_questions_then_answers_then_none_and_ai_label_scope_all(fresh, results_log):
    """First preview asks all five questions; apply without answers is refused; apply with
    answers (aiLabel scope all) labels the created test, Pre-Condition and Test Set; the answers
    are remembered in the temp preferences file and a second preview asks nothing."""
    lv = fresh
    log = results_log.setdefault("importChoices", {})
    ai = "ai-" + RUN_ID
    rel = write_feature(lv, "questions.feature", (
        "@smoke\nFeature: %s\n\n  Background: %s\n    Given the app is running\n\n"
        "  Scenario: %s\n    When I do one\n    Then it works\n"
        % (lv.summary("questions"), lv.summary("questions setup"),
           lv.summary("questions one"))))

    first = lv.call("create_tests_from_feature", featureFilePath=rel)
    assert {q["id"] for q in first["questions"]} == QUESTION_IDS, first["questions"]
    assert all(q["question"] for q in first["questions"])
    refused = lv.call_error("create_tests_from_feature", featureFilePath=rel, apply=True)
    assert {q["id"] for q in refused["questions"]} == QUESTION_IDS, refused
    assert not lv.created, "a refused apply must write nothing"

    answers = {"createTestSet": True, "backgrounds": "preconditions", "tagsAsLabels": True,
               "aiLabel": ai, "aiLabelScope": "all"}
    answered = lv.call("create_tests_from_feature", featureFilePath=rel, **answers)
    assert answered["questions"] == [], answered["questions"]
    res = lv.call("create_tests_from_feature", featureFilePath=rel, apply=True, **answers)
    test_key = res["tests"][0]["key"]
    pre_key = res["preconditions"][0]["key"]
    assert res["preconditions"][0]["created"] and res["testSet"]["created"], res
    set_key = res["testSet"]["key"]

    labels = {k: issue(lv, q, k)["jira"]["labels"] for q, k in (
        ("getTests", test_key), ("getPreconditions", pre_key), ("getTestSets", set_key))}
    for k, got in labels.items():
        assert ai in got, (k, got)
    assert "smoke" in labels[test_key] and LIVE_LABEL in labels[test_key], labels
    assert members(lv, set_key) == [test_key]

    prefs_file = Path(lv.env["CLAUDE_PLUGIN_DATA"]) / "preferences.json"
    stored = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert stored == {"%s/%s" % (region_of(lv.ctx), lv.project): answers}, stored

    second = lv.call("create_tests_from_feature", featureFilePath=rel)
    assert second["questions"] == [], second["questions"]
    assert {n: second["choices"][n]["source"] for n in QUESTION_IDS} == {
        n: "remembered" for n in QUESTION_IDS}
    log["questions"] = ("ok: first preview asks createTestSet, backgrounds, tagsAsLabels, "
                        "aiLabel, aiLabelScope; apply without answers refused (nothing "
                        "written); answers remembered on apply in the temp preferences file; "
                        "second preview asks nothing (all sources remembered)")
    log["aiLabelScope all"] = ("ok: AI label on the created test, Pre-Condition and Test Set "
                               "(read back via Xray)")


def test_tag_rules_labels_test_sets_fields_and_never_overwrite(fresh, results_log):
    """key=value, key:value and comma-list tags through rules: labels, a reused Test Set
    (created beforehand) and a created one, a field value; memberships and fields read back via
    Xray. Then a re-import with @TEST_ keys: an already-set field is never overwritten (notDone),
    an empty one is set through Jira."""
    lv = fresh
    log = results_log.setdefault("tagRules", {})
    set_prefix = lv.summary("suite")
    # Without XRAY_TEST_DATE_FIELD the @start tag is skipped instead of mapped to a field.
    fields = ("    fields:\n      test:\n        startDate: {id: %s, type: string}\n"
              % START_FIELD) if START_FIELD else ""
    start_rule = ("{tag: start, field: {startDate: \"{value}\"}}" if START_FIELD
                  else "{tag: start, skip: true}")
    settings(lv, (
        "projects:\n  %s:\n%s"
        "    featureImport:\n      tagRules:\n"
        "        - {tag: component, label: \"comp-{value}\"}\n"
        "        - {tag: priority, label: \"prio-{value}\"}\n"
        "        - {tag: suite, split: \",\", testSet: \"%s {value}\"}\n"
        "        - %s\n"
        "        - {match: \"wip\", skip: true}\n") % (lv.project, fields, set_prefix,
                                                    start_rule))
    smoke = lv.call("create_test_set", summary="%s smoke" % set_prefix)["key"]

    text = ("Feature: %s\n\n"
            "  @component=orders @priority:high @suite=smoke,regression @start=2026-12-01 "
            "@fast @wip\n  Scenario: %s\n    Given one\n\n"
            "  @suite=smoke\n  Scenario: %s\n    Given two\n"
            % (lv.summary("tag rules"), lv.summary("tagged one"), lv.summary("tagged two")))
    rel = write_feature(lv, "tagrules.feature", text)
    answers = {"createTestSet": False, "tagsAsLabels": True, "aiLabel": ""}

    preview = lv.call("create_tests_from_feature", featureFilePath=rel, **answers)
    assert preview["questions"] == [], preview["questions"]
    assert preview["choices"]["tagRules"] == {"count": 5, "source": "settings"}
    assert [(s["name"], s["key"]) for s in preview["testSets"]["reuse"]] == [
        ("%s smoke" % set_prefix, smoke)], preview["testSets"]
    assert [s["name"] for s in preview["testSets"]["create"]] == [
        "%s regression" % set_prefix], preview["testSets"]
    one = preview["perTest"][0]
    assert {"comp-orders", "prio-high", "fast"} <= set(one["labels"]), one
    assert not any("wip" in l or "=" in l or ":" in l for l in one["labels"]), one
    assert one["fields"] == ({"startDate": "2026-12-01"} if START_FIELD else {}), one
    assert "tagSummary" in preview

    res = lv.call("create_tests_from_feature", featureFilePath=rel, apply=True, **answers)
    k1, k2 = [t["key"] for t in res["tests"]]
    created_sets = res["testSetsCreated"]
    assert [s["name"] for s in created_sets] == ["%s regression" % set_prefix], res
    regression = created_sets[0]["key"]

    t1 = issue(lv, "getTests", k1)["jira"]
    t2 = issue(lv, "getTests", k2)["jira"]
    assert {"comp-orders", "prio-high", "fast", LIVE_LABEL} <= set(t1["labels"]), t1
    assert not any("wip" in l for l in t1["labels"]), t1
    assert members(lv, smoke) == sorted([k1, k2])
    assert members(lv, regression) == [k1]
    if not START_FIELD:
        log["new tests"] = ("ok: key=value / key:value / comma-list tags -> labels, Test Set "
                            "reused and created; field checks skipped (XRAY_TEST_DATE_FIELD "
                            "not set)")
        log["never overwrite"] = "skipped (XRAY_TEST_DATE_FIELD not set)"
        return
    # The field read used by "never overwrite": jira(fields: [<id>]) returns the value under
    # the field id.
    assert START_FIELD in t1, sorted(t1)
    assert t1[START_FIELD] == "2026-12-01", t1
    assert t2.get(START_FIELD) in (None, ""), t2
    log["new tests"] = ("ok: key=value / key:value / comma-list tags -> labels comp-orders, "
                        "prio-high (+ unmatched 'fast' kept, 'wip' skipped), Test Set "
                        "'<prefix> smoke' reused (created beforehand) with both tests, "
                        "'<prefix> regression' created with one, startDate (%s) set on "
                        "create; read back via Xray" % START_FIELD)
    log["field read key"] = ("ok: Xray jira(fields: [\"%s\"]) returns the value under the "
                             "field id key (\"2026-12-01\")" % START_FIELD)

    if not HAS_JIRA:
        log["never overwrite"] = "skipped (no Jira token)"
        return
    text2 = text.replace("@start=2026-12-01", "@TEST_%s @start=2026-12-15" % k1)
    text2 = text2.replace("  @suite=smoke\n", "  @TEST_%s @suite=smoke @start=2026-12-20\n" % k2)
    assert text2 != text
    write_feature(lv, "tagrules.feature", text2)
    preview2 = lv.call("create_tests_from_feature", featureFilePath=rel, **answers)
    assert [a["key"] for a in preview2["actions"] if a["kind"] == "update_test"] == [k1, k2]
    nd = [n for n in preview2["notDone"] if n["item"] == k1]
    assert len(nd) == 1 and "already set" in nd[0]["reason"] and "2026-12-01" in nd[0]["reason"], \
        preview2["notDone"]
    assert not [n for n in preview2["notDone"] if n["item"] == k2], preview2["notDone"]
    res2 = lv.call("create_tests_from_feature", featureFilePath=rel, apply=True, **answers)
    assert res2["created"] == [] and sorted(res2["updated"]) == sorted([k1, k2]), res2
    assert res2.get("fieldsApplied") == [{"test": k2, "fields": ["startDate"]}], res2
    assert issue(lv, "getTests", k1)["jira"][START_FIELD] == "2026-12-01"
    assert issue(lv, "getTests", k2)["jira"][START_FIELD] == "2026-12-20"
    assert members(lv, smoke) == sorted([k1, k2]) and members(lv, regression) == [k1]
    log["never overwrite"] = ("ok: re-import with @TEST_ keys: set field listed in notDone "
                              "(already set to 2026-12-01, unchanged); empty field set via "
                              "Jira (2026-12-20); Test Sets reused, memberships unchanged")


def _step_shape(steps) -> list:
    return [(st.text, st.data_table, st.doc_string.content if st.doc_string else None,
             st.doc_string.media_type if st.doc_string else None) for st in steps]


def test_inline_backgrounds_read_back(fresh, results_log):
    """backgrounds=inline: no Pre-Conditions; each test starts with the feature Background,
    then the rule Background, with tables and doc strings intact (read back via Xray)."""
    lv = fresh
    log = results_log.setdefault("importChoices", {})
    dst = Path(lv.env["CLAUDE_PROJECT_DIR"]) / lv.run_id / "inline" / "inline_backgrounds.feature"
    dst.parent.mkdir(parents=True, exist_ok=True)
    prefixed_copy(REGRESSION / "inline_backgrounds.feature", dst, lv.summary("inline").strip())
    rel = str(dst.relative_to(lv.env["CLAUDE_PROJECT_DIR"]))
    feature = parse_feature(dst.read_text(encoding="utf-8"), rel)
    res = lv.call("create_tests_from_feature", featureFilePath=rel, apply=True,
                  createTestSet=False, backgrounds="inline", tagsAsLabels=True, aiLabel="")
    assert not res.get("preconditions"), res
    assert len(res["tests"]) == len(feature.scenarios) == 2
    for t in res["tests"]:
        s = feature.scenarios[t["index"]]
        got = lv.call("get_test_steps", testKey=t["key"], forceRefresh=True)
        back = reparse(got["gherkin"], s, feature.language)
        want = [st for b in s.backgrounds for st in b.steps] + list(s.steps)
        assert _step_shape(back.steps) == _step_shape(want), (t["key"], got["gherkin"])
        assert [(e.header, e.rows) for e in back.examples] == [
            (e.header, e.rows) for e in s.examples]
    assert len(feature.scenarios[1].backgrounds) == 2  # feature + rule Background
    log["inline backgrounds"] = ("ok: no Pre-Conditions; feature then rule Background steps at "
                                 "the start of each test; tables (incl. escaped pipe) and doc "
                                 "strings (media type, blank/indented lines, ``` with \"\"\") "
                                 "intact")


def test_analyze_feature_tags_regression_folder(fresh, results_log):
    lv = fresh
    out = lv.call("analyze_feature_tags", folder=str(REGRESSION))
    want = sorted(p.name for p in REGRESSION.glob("*.feature"))
    assert sorted(Path(f).name for f in out["files"]) == want and out["errors"] == [], out
    summary = out["tagSummary"]
    assert {"suite", "area", "areas", "comp"} <= set(summary["keyValue"]), summary["keyValue"]
    assert {"team", "wip"} <= set(summary["plain"]), summary["plain"]
    assert out["rules"]["count"] == 0
    assert summary["unmatched"], summary
    results_log.setdefault("analyzeFeatureTags", {})["regression folder"] = (
        "ok: %d files, 0 errors, keyValue keys %s" % (len(want), sorted(summary["keyValue"])))


def test_get_status_check_no_secrets(fresh, results_log):
    lv = fresh
    res = REGISTRY.call(lv.ctx, "get_status", {"check": True})
    assert not res.get("isError"), "get_status failed"
    text = res["content"][0]["text"]
    leaked = [n for n in SECRET_ENV if (os.environ.get(n) or "").strip()
              and os.environ[n].strip() in text]
    assert leaked == [], "get_status output contains the value of %s" % leaked
    out = json.loads(text)
    assert out["credentials"] == {"configured": True}
    assert out["projects"] == [lv.project]
    assert out["preferencesFile"].startswith(lv.env["CLAUDE_PLUGIN_DATA"])
    assert set(out["perProject"][lv.project]["unanswered"]) == {
        "createTestSet", "backgrounds", "tagsAsLabels", "aiLabel", "aiLabelScope"}
    if HAS_JIRA:
        assert out["jira"]["configured"] and out["jira"]["reachable"] is True, out["jira"]
    results_log.setdefault("getStatus", {})["check"] = (
        "ok: credentials configured (no values), Jira reachable via /myself, preferences file "
        "in the temp plugin data dir, all five choices unanswered; no secret value in output")
