"""Write live checks against XRAY_TEST_PROJECT. Every issue created here is deleted at the end of
the session (see conftest.Live.cleanup). Outcomes are collected in results_log for
docs/coverage.md."""
from __future__ import annotations

import io
import json
import os
import re
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from xray_cloud._vendor_path import import_vendored
from xray_cloud.gherkin.parse import ParsedScenario, normalise_text, parse_feature
from xray_cloud.gherkin.serialize import background_definition

import_vendored("gherkin")
from gherkin.dialect import Dialect  # noqa: E402

LIVE_LABEL = "xray-cloud-live-test"  # same as conftest.LIVE_LABEL
REGRESSION = Path(__file__).resolve().parents[1] / "fixtures" / "gherkin" / "regression"
# Explicit answers to the import questions (the tools ask for them otherwise); "" = no AI label.
FEATURE_ANSWERS = {"createTestSet": True, "backgrounds": "preconditions", "tagsAsLabels": True,
                   "aiLabel": ""}
ENV_NAME = "xray-cloud-live"  # Xray cannot delete environments: one fixed name, reused
GQL_STEPS = """
query S($jql: String!) { getTests(jql: $jql, limit: 1) { results {
  issueId scenarioType gherkin testType { name kind }
  steps { id action data result attachments { id filename downloadLink } }
  jira(fields: ["key", "summary", "labels", "issuelinks"]) } } }
"""


def png(color: int = 0) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"\x00" + bytes([color, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def raw_test(lv, key: str) -> dict:
    rows = lv.gql(GQL_STEPS, {"jql": "issue = %s" % key})["getTests"]["results"]
    assert rows, key
    return rows[0]


def actions(lv, key: str) -> List[str]:
    return [s["action"] for s in lv.call("get_test_steps", testKey=key,
                                         forceRefresh=True)["steps"]]


# ---------------------------------------------------------------------------------------------
# Manual tests and steps
# ---------------------------------------------------------------------------------------------

def test_manual_test_steps_insert_move_attachments(wlive, tmp_path, results_log):
    lv = wlive
    log = results_log.setdefault("steps", {})
    res = lv.call("create_test", aiLabel="", summary=lv.summary("manual steps"),
                  testType="Manual",
                  description="created by the live test suite",
                  steps=[{"action": "A", "data": "da", "result": "ra"},
                         {"action": "B", "result": "rb"}, {"action": "C", "result": "rc"}])
    key = res["key"]
    lv.track("test", key)
    assert res["created"] and res["testType"] == "Manual"
    got = lv.call("get_test", testKey=key, detail=True)
    assert got["testType"] == "Manual" and got["summary"] == lv.summary("manual steps")
    steps = lv.call("get_test_steps", testKey=key, forceRefresh=True)["steps"]
    assert [(s["action"], s["data"], s["result"]) for s in steps] == [
        ("A", "da", "ra"), ("B", "", "rb"), ("C", "", "rc")]
    log["createTest steps"] = "ok"

    shot = tmp_path / "shot.png"
    data = png(200)
    shot.write_bytes(data)
    res = lv.call("add_test_steps", testKey=key,
                  steps=[{"action": "D", "result": "rd", "attachments": [str(shot)]}])
    assert res["added"] == [4] and res["steps"][3]["attachments"] == ["shot.png"]

    res = lv.call("add_test_steps", testKey=key, position={"at": "after", "stepNumber": 1},
                  steps=[{"action": "X", "result": "rx"}])
    assert [s["action"] for s in res["steps"]] == ["A", "X", "B", "C", "D"]
    assert res["steps"][4]["attachments"] == ["shot.png"]
    log["insert after"] = "ok (tail rebuilt, attachment carried over)"

    res = lv.call("move_test_step", testKey=key, fromStep=5, toStep=1)
    assert [s["action"] for s in res["steps"]] == ["D", "A", "X", "B", "C"]
    assert res["steps"][0]["attachments"] == ["shot.png"]
    att = raw_test(lv, key)["steps"][0]["attachments"][0]
    assert lv.ctx.xray.download(att["downloadLink"]) == data
    log["move_test_step"] = "ok"
    log["attachment download"] = "ok (bytes identical after re-upload)"

    shot2 = tmp_path / "embed.png"
    shot2.write_bytes(png(10))
    res = lv.call("edit_test_step", testKey=key, stepNumber=2, data="new data",
                  attachments=[str(shot2)], embedAttachmentsIn="result")
    step2 = res["steps"][1]
    assert step2["data"] == "new data" and step2["attachments"] == ["embed.png"]
    assert "!xray-attachment://" in step2["result"]
    log["updateTestStep + embed"] = "ok"

    res = lv.call("remove_test_step", testKey=key, stepNumber=3)
    assert res["removed"]["action"] == "X"
    assert [s["action"] for s in res["steps"]] == ["D", "A", "B", "C"]

    res = lv.call("delete_test", testKey=key)
    assert "wouldDelete" in res
    assert lv.call("delete_test", testKey=key, confirm=True)["deleted"]
    lv.untrack(key)
    assert not lv.call("search_tests", query=key)["tests"]
    log["deleteTest"] = "ok"


def test_test_types_gherkin_and_jira_edits(wlive, results_log):
    lv = wlive
    log = results_log.setdefault("tests", {})
    gherkin = "Given a thing\nWhen it runs\nThen it works"
    res = lv.call("create_test", aiLabel="", summary=lv.summary("cucumber"),
                  testType="Cucumber", gherkin=gherkin)
    ck = res["key"]
    lv.track("test", ck)
    t = raw_test(lv, ck)
    assert t["testType"]["name"] == "Cucumber" and t["gherkin"].strip() == gherkin
    lv.call("edit_test", testKey=ck, gherkin=gherkin + "\nAnd more")
    assert raw_test(lv, ck)["gherkin"].strip().endswith("And more")
    log["createTest gherkin / updateGherkinTestDefinition"] = "ok"

    res = lv.call("create_test", aiLabel="", summary=lv.summary("generic"), testType="Generic")
    gk = res["key"]
    lv.track("test", gk)
    lv.call("update_test_type", testKey=gk, testType="Manual")
    assert raw_test(lv, gk)["testType"]["name"] == "Manual"
    log["updateTestType"] = "ok"

    if lv.ctx.jira is None:
        log["edit_test jira"] = "not run (no Jira token)"
        return
    lv.call("edit_test", testKey=gk, summary=lv.summary("generic renamed"),
            description="line one\n\nline three")
    issue = lv.jira("GET", "/rest/api/3/issue/%s" % gk, params={"fields": "summary,description"})
    assert issue["fields"]["summary"] == lv.summary("generic renamed")
    paras = issue["fields"]["description"]["content"]
    texts = ["".join(c.get("text", "") for c in p.get("content") or []) for p in paras]
    assert texts == ["line one", "", "line three"], texts
    log["edit_test summary/description (ADF)"] = "ok"

    lv.call("edit_test", testKey=gk, labels=["keep-me", LIVE_LABEL])
    lv.call("set_lifecycle_label", testKey=gk, label="ai-draft")
    res = lv.call("set_lifecycle_label", testKey=gk, label="automated")
    assert res["labels"] == ["keep-me", LIVE_LABEL, "automated"]
    issue = lv.jira("GET", "/rest/api/3/issue/%s" % gk, params={"fields": "labels"})
    assert sorted(issue["fields"]["labels"]) == sorted(["automated", "keep-me", LIVE_LABEL])
    log["set_lifecycle_label"] = "ok"


def test_duplicate_check_with_special_characters(wlive, results_log):
    lv = wlive
    s = lv.summary("dup [check] (x) \"q\"")
    first = lv.call("create_test", aiLabel="", summary=s, testType="Manual")
    lv.track("test", first["key"])
    second = lv.call("create_test", aiLabel="", summary=s, testType="Manual")
    assert second["status"] == "possible_duplicates"
    assert first["key"] in [c["key"] for c in second["candidates"]]
    results_log.setdefault("tests", {})["duplicate check"] = "ok (brackets/quotes in summary)"


# ---------------------------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------------------------

def test_folders(wlive, results_log):
    lv = wlive
    log = results_log.setdefault("folders", {})
    base = "/" + lv.run_id
    lv.folders.append(base)
    t1 = lv.call("create_test", aiLabel="", summary=lv.summary("folder 1"),
                 testType="Manual")["key"]
    t2 = lv.call("create_test", aiLabel="", summary=lv.summary("folder 2"), testType="Manual",
                 folderPath=base + "/A/B")["key"]
    lv.track("test", t1, t2)
    assert lv.call("create_folder", path=base + "/A")["created"]
    assert lv.call("create_folder", path=base + "/A")["created"]  # idempotent
    lv.call("add_tests_to_folder", path=base + "/A", testKeys=[t1])
    res = lv.call("get_tests_in_folder", path=base + "/A")
    assert [t["key"] for t in res["tests"]] == [t1], res
    assert [t["key"] for t in lv.call("get_tests_in_folder", path=base + "/A/B")["tests"]] == [t2]
    pid = lv.ctx.project_ids[lv.project]
    q = ("query F($p: String!, $path: String!, $d: Boolean) { getTests(projectId: $p, "
         "folder: {path: $path, includeDescendants: $d}, limit: 10) { results { jira(fields: [\"key\"]) } } }")
    keys = lambda d: sorted(r["jira"]["key"] for r in lv.gql(q, {"p": pid, "path": base + "/A", "d": d})["getTests"]["results"])
    assert keys(False) == [t1] and keys(True) == sorted([t1, t2])
    log["getTests folder filter"] = "ok (includeDescendants false/true)"
    tree = lv.call("list_folders", path=base)["tree"]
    assert [c["path"] for c in tree["children"]] == [base + "/A"]

    lv.call("rename_folder", path=base + "/A/B", newName="C")
    assert [t["key"] for t in lv.call("get_tests_in_folder", path=base + "/A/C")["tests"]] == [t2]
    log["renameFolder"] = "ok"
    lv.call("move_folder", path=base + "/A/C", destinationPath=base)
    assert [t["key"] for t in lv.call("get_tests_in_folder", path=base + "/C")["tests"]] == [t2]
    log["moveFolder destinationPath"] = "ok (destinationPath is the new parent)"
    lv.call("move_folder", path=base + "/C", destinationPath=base + "/A")
    prev = lv.call("delete_folder", path=base)
    assert prev["wouldDelete"]["subfolders"] == 1
    lv.call("delete_folder", path=base, confirm=True)
    lv.folders.remove(base)
    gone = lv.call_error("get_tests_in_folder", path=base + "/A")["error"].lower()
    assert "no valid folder" in gone or "not found" in gone, gone
    assert raw_test(lv, t2)["jira"]["key"] == t2  # tests survive folder deletion
    log["deleteFolder with subfolders"] = "ok (tests kept)"


# ---------------------------------------------------------------------------------------------
# Sets, plans, executions, runs, environments
# ---------------------------------------------------------------------------------------------

def test_containers_execution_and_runs(wlive, tmp_path, results_log):
    lv = wlive
    log = results_log.setdefault("containers", {})
    t1 = lv.call("create_test", aiLabel="", summary=lv.summary("run 1"), testType="Manual",
                 steps=[{"action": "s1", "result": "r1"}, {"action": "s2", "result": "r2"}])["key"]
    t2 = lv.call("create_test", aiLabel="", summary=lv.summary("run 2"), testType="Manual",
                 steps=[{"action": "only", "result": "r"}])["key"]
    lv.track("test", t1, t2)

    ts = lv.call("create_test_set", aiLabel="", summary=lv.summary("set"), testKeys=[t1],
                 newTests=[{"summary": lv.summary("set new test"), "testType": "Generic"}])
    lv.track("testSet", ts["key"])
    lv.track("test", *ts.get("createdTests", []))
    assert len(ts["createdTests"]) == 1
    members = lv.call("get_tests_in_test_set", testSetKey=ts["key"])
    assert members["total"] == 2
    lv.call("add_tests_to_test_set", testSetKey=ts["key"], testKeys=[t2])
    res = lv.call("remove_tests_from_test_set", testSetKey=ts["key"],
                  testKeys=[ts["createdTests"][0]])
    assert res["removed"] == ts["createdTests"]
    assert sorted(t["key"] for t in lv.call("get_tests_in_test_set",
                                            testSetKey=ts["key"])["tests"]) == sorted([t1, t2])
    assert lv.call("search_test_sets", query=ts["key"])["testSets"][0]["key"] == ts["key"]
    log["test set create/add/remove/search"] = "ok"

    tp = lv.call("create_test_plan", aiLabel="", summary=lv.summary("plan"),
                 testSetKeys=[ts["key"]])
    lv.track("testPlan", tp["key"])
    assert lv.call("get_tests_in_test_plan", testPlanKey=tp["key"])["total"] == 2
    res = lv.call("remove_tests_from_test_plan", testPlanKey=tp["key"], testKeys=[t2])
    assert res["removed"] == [t2]
    lv.call("add_tests_to_test_plan", testPlanKey=tp["key"], testKeys=[t2])
    log["test plan create/add/remove"] = "ok"

    ex = lv.call("create_test_execution", summary=lv.summary("execution"),
                 testPlanKey=tp["key"], environments=[ENV_NAME])
    lv.track("testExecution", ex["key"])
    assert ex["testPlanKey"] == tp["key"] and len(ex["addedTests"]) == 2
    assert ENV_NAME in lv.call("get_test_environments")["environments"]
    log["test execution create (plan, environments)"] = "ok"

    res = lv.call("remove_test_environments_from_test_execution", testExecutionKey=ex["key"],
                  environments=[ENV_NAME, "never-there"])
    assert res["removed"] == [ENV_NAME] and res["notPresent"] == ["never-there"]
    res = lv.call("add_test_environments_to_test_execution", testExecutionKey=ex["key"],
                  environments=[ENV_NAME])
    assert ENV_NAME in res["associatedEnvironments"]
    log["environments add/remove/list"] = "ok"

    res = lv.call("remove_tests_from_test_execution", testExecutionKey=ex["key"], testKeys=[t2])
    assert res["removed"] == [t2]
    lv.call("add_tests_to_test_execution", testExecutionKey=ex["key"], testKeys=[t2])

    ev = tmp_path / "evidence.png"
    ev.write_bytes(png(99))
    defect = None
    if lv.ctx.jira is not None:
        meta = lv.call("get_create_fields", issueType="Bug")
        assert meta["source"] == "jira"
        created = lv.jira("POST", "/rest/api/3/issue", {"fields": {
            "project": {"key": lv.project}, "issuetype": {"name": "Bug"},
            "summary": lv.summary("defect"), "labels": [LIVE_LABEL]}})
        defect, defect_id = created["key"], created["id"]
        lv.track("jira", defect)
    args: Dict[str, Any] = dict(
        testExecutionKey=ex["key"], testKey=t1,
        steps=[{"index": 0, "status": "PASS", "comment": "fine"},
               {"index": 1, "status": "FAIL", "actualResult": "boom"}],
        attachments=[str(ev)])
    if defect:
        args["defectKeys"] = [defect]
    res = lv.call("update_test_run", **args)
    assert res["status"] == "FAILED" and res["attachmentsUploaded"] == 1
    lv.call("update_test_run", testExecutionKey=ex["key"], testKey=t2, status="PASS")
    rows = {r["testKey"]: r for r in lv.call("get_test_execution", testExecutionKey=ex["key"],
                                            detail=True)["testRuns"]}
    assert rows[t1]["status"] == "FAILED" and rows[t2]["status"] == "PASSED"
    assert rows[t1]["failedSteps"][0]["index"] == 1
    run = lv.gql("query R($e: [String], $t: [String]) { getTestRuns(testExecIssueIds: $e, "
                 "testIssueIds: $t, limit: 1) { results { evidence { filename } defects "
                 "steps { status { name } comment actualResult } } } }",
                 {"e": [lv.issue_id(ex["key"])], "t": [lv.issue_id(t1)]})["getTestRuns"]["results"][0]
    assert [e["filename"] for e in run["evidence"]] == ["evidence.png"]
    assert run["steps"][0]["comment"] == "fine" and run["steps"][1]["actualResult"] == "boom"
    log["updateTestRunStep / status PASSED+FAILED / evidence"] = "ok"
    if defect:
        assert run["defects"] == [defect_id], run["defects"]
        log["addDefectsToTestRun"] = "ok (TestRun.defects returns Jira issue ids, not keys)"
    err = lv.call_error("update_test_run", testExecutionKey=ex["key"], testKey=t1,
                        fields={"anything": 1})
    assert "Unknown field" in err["error"]
    log["run custom fields"] = "not verifiable (test project has no test-run custom fields)"

    for tool, k, name in (("delete_test_execution", "testExecutionKey", ex["key"]),
                          ("delete_test_plan", "testPlanKey", tp["key"]),
                          ("delete_test_set", "testSetKey", ts["key"])):
        assert "wouldDelete" in lv.call(tool, **{k: name})
        assert lv.call(tool, confirm=True, **{k: name})["deleted"]
        lv.untrack(name)
    log["delete set/plan/execution"] = "ok"


# ---------------------------------------------------------------------------------------------
# Feature import
# ---------------------------------------------------------------------------------------------

def _keywords(language: str) -> List[str]:
    d = Dialect.for_name(language)
    out = []
    for kws in (d.feature_keywords, d.scenario_keywords, d.scenario_outline_keywords,
                d.background_keywords):
        out.extend(k for k in kws if k.strip() and k.strip() != "*")
    return sorted(set(out), key=len, reverse=True)


def prefixed_copy(src: Path, dst: Path, prefix: str) -> None:
    """Copy a feature, inserting `prefix` into every Feature/Scenario/Background name
    (unnamed Backgrounds stay unnamed). BOM, line endings and everything else are kept."""
    raw = src.read_bytes().decode("utf-8")
    m = re.search(r"^\s*#\s*language:\s*(\S+)", raw, re.M)
    kws = _keywords(m.group(1) if m else "en")
    out, in_doc = [], None
    for line in raw.splitlines(keepends=True):
        body = line.lstrip("﻿").strip()
        if in_doc:
            if body.startswith(in_doc):
                in_doc = None
        elif body.startswith('"""') or body.startswith("```"):
            in_doc = body[:3]
        else:
            for kw in kws:
                if body.startswith(kw + ":"):
                    rest = body[len(kw) + 1:]
                    if rest.strip() or kw not in Dialect.for_name(m.group(1) if m else "en").background_keywords:
                        i = line.index(kw + ":") + len(kw) + 1
                        line = line[:i] + " " + prefix + line[i:]
                    break
        out.append(line)
    dst.write_bytes("".join(out).encode("utf-8"))


def reparse(definition: str, s: ParsedScenario, language: str) -> ParsedScenario:
    d = Dialect.for_name(language)
    head = "# language: %s\n" % language if language != "en" else ""
    if language != "en":  # Xray stores the English Examples keyword (see serialize.py)
        definition = re.sub(r"(?m)^(\s*)Examples:", lambda m: m.group(1) + d.examples_keywords[0]
                            + ":", definition)
    body = "\n".join(("    " + l) if l.strip() else "" for l in normalise_text(definition).split("\n"))
    text = "%s%s: x\n\n  %s: y\n%s\n" % (head, d.feature_keywords[0], s.keyword, body)
    return parse_feature(text, "readback").scenarios[0]


def shape(s: ParsedScenario) -> dict:
    return {"steps": [(st.keyword.strip(), st.text, st.data_table,
                       st.doc_string.content if st.doc_string else None,
                       st.doc_string.media_type if st.doc_string else None) for st in s.steps],
            "examples": [(e.name, e.tags, e.header, e.rows, e.description) for e in s.examples]}


def import_feature(lv, name: str, sub: str = "") -> tuple:
    dst_dir = Path(lv.env["CLAUDE_PROJECT_DIR"]) / lv.run_id / sub
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    # each import of the same file gets its own prefix, or the duplicate check skips it
    prefixed_copy(REGRESSION / name, dst, lv.summary(sub).strip())
    rel = str(dst.relative_to(lv.env["CLAUDE_PROJECT_DIR"]))
    res = lv.call("create_tests_from_feature", featureFilePath=rel, apply=True,
                  **FEATURE_ANSWERS)
    lv.track("test", *[t["key"] for t in res.get("tests", [])])
    lv.track("precondition", *[p["key"] for p in res.get("preconditions", []) if p.get("created")])
    if (res.get("testSet") or {}).get("created"):
        lv.track("testSet", res["testSet"]["key"])
    return res, dst, rel


@pytest.mark.parametrize("name", sorted(p.name for p in REGRESSION.glob("*.feature")))
def test_feature_import_round_trip(wlive, results_log, name):
    lv = wlive
    log = results_log.setdefault("featureImport", {})
    res, path, rel = import_feature(lv, name)
    feature = parse_feature(path.read_text(encoding="utf-8"), rel)
    assert not res.get("skippedDuplicates"), res
    assert len(res["tests"]) == len(feature.scenarios)
    notes = []
    for t in res["tests"]:
        s = feature.scenarios[t["index"]]
        got = lv.call("get_test_steps", testKey=t["key"], forceRefresh=True)
        assert "gherkin" in got, got
        back = reparse(got["gherkin"], s, feature.language)
        assert shape(back) == shape(s), (t["key"], got["gherkin"])
        rt = raw_test(lv, t["key"])
        assert rt["testType"]["name"] == "Cucumber"
        want = "scenario_outline" if s.is_outline else "scenario"
        st = (rt.get("scenarioType") or "").lower().replace(" ", "_")
        assert st == want, (t["key"], rt.get("scenarioType"))
        if s.is_outline:
            notes.append("outline %s scenarioType=%s" % (t["key"], rt.get("scenarioType")))
    for p in res.get("preconditions", []):
        bg_def = None
        rows = lv.gql('query P($j: String!) { getPreconditions(jql: $j, limit: 1) { results { '
                      'definition preconditionType { name } tests(limit: 50) { results { '
                      'jira(fields: ["key"]) } } } } }', {"j": "issue = %s" % p["key"]}
                      )["getPreconditions"]["results"][0]
        assert rows["preconditionType"]["name"] == "Cucumber"
        assert sorted(r["jira"]["key"] for r in rows["tests"]["results"]) == sorted(p["tests"])
        for bg in feature.backgrounds:
            if background_definition(bg).strip() == normalise_text(rows["definition"]).strip():
                bg_def = bg
        assert bg_def is not None, (p["key"], rows["definition"])
    log[name] = "ok: %d tests, %d Pre-Conditions%s%s" % (
        len(res["tests"]), len(res.get("preconditions", [])),
        ", Test Set" if res.get("testSet", {}).get("created") else "",
        (" (" + "; ".join(notes) + ")") if notes else "")


def test_feature_reimport_reuses_and_updates(wlive, results_log):
    """Second import of the same file: Pre-Condition found by path label and Test Set by
    summary; @TEST_ tags update tests; a changed Background updates the Pre-Condition."""
    lv = wlive
    log = results_log.setdefault("featureImport", {})
    res, path, rel = import_feature(lv, "sample.feature", sub="reimport")
    keys = [t["key"] for t in res["tests"]]
    pre = res["preconditions"][0]["key"]
    text = path.read_text(encoding="utf-8")
    text = text.replace("Given the app is running", "Given the app is running again")
    lines, n = [], 0
    for line in text.split("\n"):
        if line.strip().startswith(("Scenario:", "Scenario Outline:")):
            lines.append("  @TEST_%s" % keys[n])
            n += 1
        lines.append(line)
    path.write_text("\n".join(lines), encoding="utf-8")
    preview = lv.call("create_tests_from_feature", featureFilePath=rel, **FEATURE_ANSWERS)
    kinds = {a["kind"]: a for a in preview["actions"]}
    assert kinds["reuse_precondition"]["key"] == pre
    assert kinds["reuse_precondition"].get("updateDefinition") is True
    assert kinds["reuse_test_set"]["key"] == res["testSet"]["key"]
    assert [a["key"] for a in preview["actions"] if a["kind"] == "update_test"] == keys
    res2 = lv.call("create_tests_from_feature", featureFilePath=rel, apply=True,
                   **FEATURE_ANSWERS)
    assert res2["created"] == [] and sorted(res2["updated"]) == sorted(keys + [pre])
    d = lv.gql('query P($j: String!) { getPreconditions(jql: $j, limit: 1) { results { '
               'definition } } }', {"j": "issue = %s" % pre})["getPreconditions"]["results"][0]
    assert d["definition"].strip() == "Given the app is running again"
    log["re-import"] = ("ok: Pre-Condition reused by path label and definition updated, Test Set "
                        "reused by summary, @TEST_ tags update tests")


def test_requirement_link_direction(jlive, results_log):
    lv = jlive
    log = results_log.setdefault("jira", {})
    story = lv.jira("POST", "/rest/api/3/issue", {"fields": {
        "project": {"key": lv.project}, "issuetype": {"name": "Story"},
        "summary": lv.summary("requirement"), "labels": [LIVE_LABEL]}})["key"]
    lv.track("jira", story)

    def links(key: str) -> List[dict]:
        issue = lv.jira("GET", "/rest/api/3/issue/%s" % key, params={"fields": "issuelinks"})
        out = []
        for ln in issue["fields"]["issuelinks"]:
            if ln["type"]["name"] != "Test":
                continue
            if "outwardIssue" in ln:
                out.append({"desc": ln["type"]["outward"], "other": ln["outwardIssue"]["key"]})
            else:
                out.append({"desc": ln["type"]["inward"], "other": ln["inwardIssue"]["key"]})
        return out

    # 1) create path: issuelinks update sent with createTest (feature import, @REQ_ tag)
    d = Path(lv.env["CLAUDE_PROJECT_DIR"]) / lv.run_id / "req"
    d.mkdir(parents=True, exist_ok=True)
    (d / "req.feature").write_text(
        "Feature: %s\n\n  @REQ_%s\n  Scenario: %s\n    Given linked\n"
        % (lv.summary("req feature"), story, lv.summary("req scenario")), encoding="utf-8")
    res = lv.call("create_tests_from_feature",
                  featureFilePath="%s/req/req.feature" % lv.run_id, apply=True,
                  **dict(FEATURE_ANSWERS, createTestSet=False))
    created = res["tests"][0]["key"]
    lv.track("test", created)
    assert links(created) == [{"desc": "tests", "other": story}], links(created)
    log["createTest issuelinks (requirement)"] = "ok: test shows 'tests %s'" % "<story>"

    # 2) existing test: JiraClient.link_requirement
    other = lv.call("create_test", aiLabel="", summary=lv.summary("req link"),
                    testType="Manual")["key"]
    lv.track("test", other)
    lv.ctx.jira.link_requirement(other, story)
    got = links(other)
    log["link_requirement direction"] = "test shows %s" % got
    assert got == [{"desc": "tests", "other": story}], got
    assert story in lv.ctx.jira.linked_keys(other)


# ---------------------------------------------------------------------------------------------
# Xray native import comparison and Background in result import
# ---------------------------------------------------------------------------------------------

def _export(lv, keys: List[str]) -> Dict[str, str]:
    data = lv.ctx.xray.download("export/cucumber?keys=" + ";".join(keys))  # GET: retried
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"export.feature": data.decode("utf-8")}
    return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def _summary_of_export(files: Dict[str, str]) -> dict:
    out = {"scenarios": [], "backgrounds": [], "tags": []}
    for name, text in sorted(files.items()):
        f = parse_feature(text, name)
        for s in f.scenarios:
            out["scenarios"].append({"name": re.sub(r"^.*?\] \S+ (ours|native) ", "", s.name),
                                     "outline": s.is_outline, "shape": shape(s),
                                     "tags": sorted(t for t in s.tags if not re.match(r"@(TEST_)?[A-Z]+-\d+$", t))})
        for b in f.backgrounds:
            out["backgrounds"].append(background_definition(b))
    out["scenarios"].sort(key=lambda s: s["name"])
    return out


def test_compare_with_native_import(wlive, results_log):
    lv = wlive
    log = results_log.setdefault("nativeImport", {})
    ours, path, rel = import_feature(lv, "sample.feature", sub="ours")
    native_src = Path(lv.env["CLAUDE_PROJECT_DIR"]) / lv.run_id / "native.feature"
    prefixed_copy(REGRESSION / "sample.feature", native_src, lv.summary("native").strip())
    body = lv.ctx.xray.rest("POST", "import/feature", params={"projectKey": lv.project},
                            multipart={"files": {"file": ("sample.feature",
                                                          native_src.read_bytes(),
                                                          "text/plain")}})
    nat_tests = [t["key"] for t in (body.get("updatedOrCreatedTests") or [])]
    nat_pre = [t["key"] for t in (body.get("updatedOrCreatedPreconditions") or [])]
    lv.track("test", *nat_tests)
    lv.track("precondition", *nat_pre)
    log["native response keys"] = sorted(k for k in body if body[k])
    assert len(nat_tests) == 2, body
    ours_keys = [t["key"] for t in ours["tests"]]
    a = _summary_of_export(_export(lv, ours_keys))
    b = _summary_of_export(_export(lv, nat_tests))
    ours_pre = lv.gql('query P($j: String!) { getPreconditions(jql: $j, limit: 5) { results { '
                      'definition jira(fields: ["key", "summary", "labels"]) } } }',
                      {"j": "issue in (%s)" % ", ".join([ours["preconditions"][0]["key"]] + nat_pre)}
                      )["getPreconditions"]["results"]
    labels = {r["jira"]["key"]: r["jira"]["labels"] for r in ours_pre}
    defs = {r["jira"]["key"]: r["definition"].strip() for r in ours_pre}
    test_labels = {}
    for k in ours_keys + nat_tests:
        test_labels[k] = raw_test(lv, k)["jira"]["labels"]
    log["ours"] = {"export": a, "preconditionLabels": labels.get(ours["preconditions"][0]["key"]),
                   "testLabels": [test_labels[k] for k in ours_keys]}
    log["native"] = {"export": b, "preconditionLabels": [labels.get(k) for k in nat_pre],
                     "preconditionDefinitions": [defs.get(k) for k in nat_pre],
                     "testLabels": [test_labels[k] for k in nat_tests]}
    assert [s["shape"] for s in a["scenarios"]] == [s["shape"] for s in b["scenarios"]]
    assert [s["outline"] for s in a["scenarios"]] == [s["outline"] for s in b["scenarios"]]
    assert defs[ours["preconditions"][0]["key"]] == defs[nat_pre[0]]
    log["result"] = "scenarios (steps, tables, doc strings, examples) and Pre-Condition steps equal"


def test_background_in_cucumber_result_import(wlive, results_log):
    """Forum issue: do Background steps show up in a run created by a Cucumber JSON import?"""
    lv = wlive
    log = results_log.setdefault("cucumberResults", {})
    res, path, rel = import_feature(lv, "sample.feature", sub="results")
    key = res["tests"][0]["key"]
    step = lambda kw, name: {"keyword": kw, "name": name, "line": 1, "match": {"location": "x"},
                             "result": {"status": "passed", "duration": 1000000}}
    report = [{"uri": "features/sample.feature", "id": "login", "keyword": "Feature",
               "name": "Login", "line": 1, "description": "", "tags": [],
               "elements": [
                   {"keyword": "Background", "name": "", "line": 3, "description": "",
                    "type": "background", "steps": [step("Given ", "the app is running")]},
                   {"id": "login;successful-login", "keyword": "Scenario",
                    "name": "Successful login", "line": 6, "description": "",
                    "type": "scenario", "tags": [{"name": "@TEST_%s" % key, "line": 5}],
                    "steps": [step("Given ", "the following users exist:"),
                              step("When ", "I enter valid credentials"),
                              step("Then ", "I am logged in")]}]}]
    out = lv.ctx.xray.rest("POST", "import/execution/cucumber", json_body=report)
    ex_key = out["key"]
    lv.track("testExecution", ex_key)
    run = lv.gql("query R($e: [String]) { getTestRuns(testExecIssueIds: $e, limit: 5) { results {"
                 " status { name } test { jira(fields: [\"key\"]) } results { name "
                 "backgrounds { keyword name status { name } } steps { keyword name } } "
                 "preconditions(limit: 5) { results { definition } } } } }",
                 {"e": [lv.issue_id(ex_key)]})["getTestRuns"]["results"]
    assert run, out
    r = run[0]
    bgs = [b for x in r.get("results") or [] for b in x.get("backgrounds") or []]
    log["status"] = r["status"]["name"]
    log["backgroundStepsInRunResults"] = [(b["keyword"], b["name"]) for b in bgs]
    log["runPreconditions"] = [p["definition"] for p in r["preconditions"]["results"]]
    log["result"] = ("Background steps appear in the run results" if bgs
                     else "Background steps do NOT appear in the run results")
