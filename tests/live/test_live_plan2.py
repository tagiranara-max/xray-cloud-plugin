"""Live checks for Plan 2 (XRAY_TEST_PROJECT): result import (JUnit multipart into a new
execution, Cucumber JSON into an existing one and into a new one with an environment), native
feature import and Cucumber export, run history, requirement coverage, find_related_tests and
the parity audit. Each test gets its own ServerContext (`fresh`) with a temp settings file and a
temp CLAUDE_PLUGIN_DATA; everything a test creates (including the tests Xray creates for JUnit
test cases) is tracked and deleted in its teardown, also when the test fails."""
from __future__ import annotations

import importlib.util
import io
import json
import re
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from xray_cloud.gherkin.parse import parse_feature

from .conftest import (HAS_JIRA, HAS_PROJECT, HAS_XRAY, LIVE_LABEL, RUN_ID,
                       SUMMARY_PREFIX, Live)
from .test_live_write import ENV_NAME, REGRESSION, prefixed_copy

REPO = Path(__file__).resolve().parents[2]
EXEC_RUNS = """
query ExecRuns($jql: String!) { getTestExecutions(jql: $jql, limit: 1) { results {
  issueId testEnvironments jira(fields: ["key", "summary"])
  testRuns(limit: 50) { total results { status { name } comment examples { status { name } }
    test { issueId jira(fields: ["key", "summary"]) } } } } } }
"""


class TestLive(Live):
    """A Live whose summaries carry, besides the run id, a token unique to one test, so the
    teardown can find (and delete) whatever that test created but never got to track."""
    __test__ = False

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.token = "t" + uuid.uuid4().hex[:8]

    def summary(self, text: str) -> str:  # type: ignore[override]
        return "%s %s %s %s" % (SUMMARY_PREFIX, RUN_ID, self.token, text)

    def _exists(self, key: str) -> bool:
        """The search index can still list a just-deleted issue; ask Jira directly."""
        try:
            self.jira("GET", "/rest/api/3/issue/%s" % key, params={"fields": "summary"})
            return True
        except Exception as e:
            if "404" in str(e) or "does not exist" in str(e).lower():
                return False
            raise

    def own_leftovers(self) -> List[str]:
        """Issues of the project whose summary carries this test's token."""
        jql = 'project = %s AND summary ~ "%s"' % (self.project, self.token)
        if self.ctx.jira is not None:
            data = self.jira("GET", "/rest/api/3/search/jql",
                             params={"jql": jql, "fields": "summary", "maxResults": 100})
            return [i["key"] for i in (data or {}).get("issues") or []
                    if self.token in (i.get("fields") or {}).get("summary", "")
                    and self._exists(i["key"])]
        keys: List[str] = []
        for q in self._QUERY.values():
            data = self.gql('query L($jql: String!) { %s(jql: $jql, limit: 100) '
                            '{ results { jira(fields: ["key", "summary"]) } } }' % q,
                            {"jql": jql})
            keys += [r["jira"]["key"] for r in (data.get(q) or {}).get("results") or []
                     if self.token in (r["jira"].get("summary") or "")]
        return keys


@pytest.fixture
def fresh(tmp_path):
    if not (HAS_XRAY and HAS_PROJECT):
        pytest.skip("XRAY_CLIENT_ID/SECRET and XRAY_TEST_PROJECT not set")
    lv = TestLive(tmp_path)
    assert lv.env["CLAUDE_PLUGIN_DATA"].startswith(str(tmp_path))
    yield lv
    failed = lv.cleanup()
    # Anything this test created without tracking it (e.g. a lookup failed before the tests
    # Xray made for JUnit cases were tracked) still carries the token: delete it too.
    orphans = [k for k in lv.own_leftovers() if k not in lv.denied]
    if orphans:
        lv.created = []
        lv.track("auto", *orphans)
        failed += lv.cleanup()
    left = lv.own_leftovers()
    assert not failed and not lv.denied and not left, (
        "cleanup incomplete: failed=%s denied=%s untracked orphans deleted=%s left=%s"
        % (failed, lv.denied, orphans, left))
    assert not orphans, "untracked issues had to be deleted by token: %s" % orphans


def eventually(fn: Callable[[], Any], ok: Callable[[Any], bool], timeout: float = 90) -> Any:
    """Jira's search index lags behind writes: retry a read until ok(result) or timeout."""
    end = time.time() + timeout
    while True:
        out = fn()
        if ok(out) or time.time() > end:
            return out
        time.sleep(5)


def exec_runs(lv: Live, key: str, min_runs: int = 0) -> dict:
    """The execution with its runs; retried while the JQL lookup lags behind the import."""
    def read() -> List[dict]:
        return lv.gql(EXEC_RUNS, {"jql": "issue = %s" % key})["getTestExecutions"]["results"]
    rows = eventually(read, lambda r: bool(r) and len(
        r[0]["testRuns"]["results"]) >= min_runs)
    assert rows, key
    return rows[0]


def track_run_tests(lv: TestLive, key: str, expected: int) -> List[dict]:
    """Track the tests Xray created for result entries (their summaries carry the token)."""
    runs = exec_runs(lv, key, expected)["testRuns"]["results"]
    for r in runs:
        jira = r["test"]["jira"]
        if lv.token in (jira.get("summary") or ""):
            lv.track("test", jira["key"])
    assert len(runs) == expected, runs
    return runs


def step(kw: str, name: str, status: str, error: str = "") -> dict:
    res: Dict[str, Any] = {"status": status, "duration": 1000000}
    if error:
        res["error_message"] = error
    return {"keyword": kw, "name": name, "line": 1, "match": {"location": "x"}, "result": res}


def scenario(name: str, key: str, *steps: dict, outline: bool = False,
             tagged: bool = True) -> dict:
    """A Cucumber JSON scenario; tagged=False leaves out the @TEST_ tag (matched by name)."""
    return {"id": "f;%s" % key.lower(), "keyword": "Scenario Outline" if outline else "Scenario",
            "name": name, "line": 5, "description": "", "type": "scenario",
            "tags": [{"name": "@TEST_%s" % key, "line": 4}] if tagged else [],
            "steps": list(steps)}


def cucumber(feature: str, *elements: dict) -> str:
    return json.dumps([{"uri": "features/sample.feature", "id": "login", "keyword": "Feature",
                        "name": feature, "line": 1, "description": "", "tags": [],
                        "elements": [{"keyword": "Background", "name": "", "line": 3,
                                      "description": "", "type": "background",
                                      "steps": [step("Given ", "the app is running",
                                                     "passed")]}] + list(elements)}])


# ---------------------------------------------------------------------------------------------

def test_feature_import_export_results_and_run_history(fresh, results_log):
    lv = fresh
    log = results_log.setdefault("plan2", {})

    # -- import_feature_file (Xray's native importer) on regression/sample.feature -----------
    # A copy with the run id in every name, so Xray (which matches by name) creates new issues.
    src = Path(lv.env["CLAUDE_PROJECT_DIR"]) / lv.run_id / "sample.feature"
    src.parent.mkdir(parents=True)
    prefixed_copy(REGRESSION / "sample.feature", src, lv.summary("native").strip())
    rel = str(src.relative_to(lv.env["CLAUDE_PROJECT_DIR"]))
    imported = lv.call("import_feature_file", filePath=rel)
    lv.track("test", *imported["tests"])
    lv.track("precondition", *imported["preconditions"])
    assert len(imported["tests"]) == 2 and len(imported["preconditions"]) == 1, imported
    assert imported["errors"] == [], imported
    names, summaries = {}, {}
    for key in imported["tests"]:
        row = lv.gql('query T($j: String!) { getTests(jql: $j, limit: 1) { results { '
                     'scenarioType jira(fields: ["key", "summary"]) } } }',
                     {"j": "issue = %s" % key})["getTests"]["results"][0]
        names[row["scenarioType"]] = key
        summaries[key] = row["jira"]["summary"]
        assert RUN_ID in row["jira"]["summary"]
    scen_key, outline_key = names["scenario"], names["scenario_outline"]
    log["import_feature_file"] = ("ok: response keys updatedOrCreatedTests / "
                                  "updatedOrCreatedPreconditions ([{id, key, self}]) and errors; "
                                  "2 tests (scenario, scenario_outline) + 1 Pre-Condition")

    # -- export_cucumber_features of those keys: files parse with the vendored parser --------
    out_dir = lv.root / "export"
    exported = lv.call("export_cucumber_features", directory=str(out_dir),
                       keys=imported["tests"])
    assert exported["files"], exported
    parsed = []
    for name in exported["files"]:
        f = parse_feature((out_dir / name).read_text(encoding="utf-8"), name)
        parsed += [(s.name, s.is_outline, sorted(s.tags)) for s in f.scenarios]
    assert sorted(t for _, _, tags in parsed for t in tags if t.startswith("@TEST_")) == sorted(
        "@TEST_%s" % k for k in imported["tests"]), parsed
    assert sorted(o for _, o, _ in parsed) == [False, True]
    again = lv.call_error("export_cucumber_features", directory=str(out_dir),
                          keys=imported["tests"])
    assert "overwrite" in again["error"], again
    log["export_cucumber_features"] = ("ok: %d file(s) %s, parse with the vendored parser, "
                                       "@TEST_ tags of both tests; a second export into the same "
                                       "directory is refused" % (len(exported["files"]),
                                                                 exported["files"]))

    # -- JUnit into a new execution, multipart (summary given) --------------------------------
    junit = ('<?xml version="1.0" encoding="UTF-8"?><testsuites><testsuite name="live" tests="2" '
             'failures="1"><testcase classname="xcl.%s" name="%s passes" time="0.1"/>'
             '<testcase classname="xcl.%s" name="%s fails" time="0.2"><failure message="boom">'
             'trace</failure></testcase></testsuite></testsuites>'
             % (RUN_ID, lv.summary("junit"), RUN_ID, lv.summary("junit")))
    # Neither case matches a test: nothing is uploaded until creation is allowed.
    asked = lv.call("import_execution_results", format="junit", content=junit,
                    summary=lv.summary("junit execution"))
    assert asked["status"] == "needs_answer" and "testExecKey" not in asked, asked
    assert sorted(asked["missing"]) == sorted("xcl.%s.%s %s" % (RUN_ID, lv.summary("junit"), s)
                                              for s in ("passes", "fails")), asked
    assert [q["id"] for q in asked["questions"]] == ["allowCreateTests"], asked
    res = lv.call("import_execution_results", format="junit", content=junit,
                  summary=lv.summary("junit execution"), detail=True, allowCreateTests=True)
    ex = res["testExecKey"]
    assert ex, res
    lv.track("testExecution", ex)
    runs = track_run_tests(lv, ex, 2)
    assert res["created"] is True and "warnings" not in res, res
    assert sorted(res["createdTests"]) == sorted(r["test"]["jira"]["key"] for r in runs), res
    assert "AI label" in res["note"], res
    assert sorted(res["response"]) == ["id", "key", "self"], res["response"]
    assert res["summary"] == {"total": 2, "byStatus": {"PASSED": 1, "FAILED": 1}}, res
    got = exec_runs(lv, ex)
    assert got["jira"]["summary"] == lv.summary("junit execution")
    assert sorted((r["test"]["jira"]["summary"], r["status"]["name"]) for r in runs) == [
        (lv.summary("junit") + " fails", "FAILED"), (lv.summary("junit") + " passes", "PASSED")]
    log["junit multipart"] = ("ok: unmatched cases -> needs_answer (missing = Generic "
                              "definitions, nothing created); with allowCreateTests: parts info "
                              "+ results; response {id, key, self} (no testExecIssue); summary "
                              "from info applied; runs and byStatus match the file; the two "
                              "new tests listed in createdTests")

    # -- Cucumber JSON into that existing execution -------------------------------------------
    # The outline rows carry no @TEST_ tag: they are matched by the outline test's summary.
    outline_name = summaries[outline_key]
    report = cucumber(lv.summary("native Login"),
                      scenario("Successful login", scen_key, step("Then ", "I am logged in",
                                                                  "passed")),
                      scenario(outline_name, outline_key,
                               step("Then ", "I see error", "passed"), outline=True,
                               tagged=False),
                      scenario(outline_name, outline_key,
                               step("Then ", "I see error", "failed", "expected X"),
                               outline=True, tagged=False))
    res = lv.call("import_execution_results", format="cucumber", content=report,
                  testExecKey=ex)
    assert res["testExecKey"] == ex and res["created"] is False, res
    assert "converted from cucumber" in res["sentAs"] and "createdTests" not in res, res
    assert res["summary"] == {"total": 4, "byStatus": {"PASSED": 2, "FAILED": 2}}, res
    by_key = {r["test"]["jira"]["key"]: r for r in exec_runs(lv, ex)["testRuns"]["results"]}
    assert by_key[scen_key]["status"]["name"] == "PASSED"
    assert by_key[outline_key]["status"]["name"] == "FAILED"
    assert "expected X" in by_key[outline_key]["comment"]
    examples = [e["status"]["name"] for e in by_key[outline_key].get("examples") or []]
    log["cucumber into existing execution"] = (
        "ok: Xray Cloud's Cucumber endpoints always create a new execution (multipart "
        "info.xrayFields.testExecKey -> HTTP 400 invalid field; testExecKey query parameter and "
        "info.key ignored); the tool sends Xray JSON with testExecutionKey: same execution, "
        "untagged outline rows matched by the test's summary, statuses per test, failure as "
        "comment, outline examples %s" % examples)
    assert examples == ["PASSED", "FAILED"], by_key[outline_key]

    # -- Cucumber JSON into a new execution with an environment (multipart) ------------------
    # Untagged, matched by summary: sent natively with an added @TEST_ tag.
    report2 = cucumber(lv.summary("native Login"),
                       scenario(summaries[scen_key], scen_key,
                                step("Then ", "I am logged in", "failed", "late"),
                                tagged=False))
    time.sleep(1)  # distinct run timestamps
    res2 = lv.call("import_execution_results", format="cucumber", content=report2,
                   testEnvironments=[ENV_NAME])
    ex2 = res2["testExecKey"]
    lv.track("testExecution", ex2)
    track_run_tests(lv, ex2, 1)
    assert res2["created"] is True and ex2 != ex and "sentAs" not in res2, res2
    assert res2["summary"] == {"total": 1, "byStatus": {"FAILED": 1}}, res2
    assert exec_runs(lv, ex2)["testEnvironments"] == [ENV_NAME]
    log["cucumber new execution + environment"] = (
        "ok: multipart (parts info + results), info.xrayFields.environments applied; an "
        "untagged scenario matched by summary is sent natively with an added @TEST_ tag")

    # -- get_test_runs: newest first, lastStatus ------------------------------------------------
    hist = lv.call("get_test_runs", testKey=scen_key)
    runs = hist["runs"]
    assert [r["testExecKey"] for r in runs] == [ex2, ex], runs
    stamps = [r["finishedOn"] or r["startedOn"] for r in runs]
    assert stamps == sorted(stamps, reverse=True), stamps
    assert runs[0]["environments"] == [ENV_NAME] and runs[1]["environments"] == []
    assert hist["summary"]["lastStatus"] == "FAILED" and hist["summary"]["flaky"] is True
    assert hist["summary"]["byStatus"] == {"FAILED": 1, "PASSED": 1}
    log["get_test_runs"] = ("ok: GraphQL fields testExecution { testEnvironments jira }, defects, "
                            "test { issueId } valid; newest first, lastStatus FAILED, flaky true, "
                            "environments per run")


def test_requirement_coverage_and_find_related(fresh, results_log):
    if not HAS_JIRA:
        pytest.skip("JIRA_SITE_URL/JIRA_EMAIL/JIRA_API_TOKEN not set")
    lv = fresh
    log = results_log.setdefault("plan2", {})
    term = "zq" + RUN_ID
    rel_label = "rel-" + RUN_ID
    test = lv.call("create_test", aiLabel="", testType="Generic",
                   summary=lv.summary("related %s" % term),
                   rawFields={"labels": [LIVE_LABEL, rel_label]})
    key = test["key"]
    lv.track("test", key)
    story = lv.jira("POST", "/rest/api/3/issue", {"fields": {
        "project": {"key": lv.project}, "issuetype": {"name": "Story"},
        "summary": lv.summary("coverage story"), "labels": [LIVE_LABEL]}})["key"]
    lv.track("jira", story)
    lv.ctx.jira.link_requirement(key, story)

    # never run yet
    cov = eventually(lambda: lv.call("get_requirement_coverage", issueKey=story),
                     lambda c: len(c["tests"]) == 1)
    assert [t["key"] for t in cov["tests"]] == [key], cov
    assert cov["tests"][0]["lastStatus"] is None and cov["counts"]["neverRun"] == 1
    assert cov["coverageStatus"] == "NOTRUN", cov
    first_status = cov["coverageStatus"]

    # a PASSED run through an Xray JSON import (plain endpoint, new execution)
    res = lv.call("import_execution_results", format="xray",
                  content=json.dumps({"info": {"summary": lv.summary("xray json")},
                                      "tests": [{"testKey": key, "status": "PASSED"}]}))
    lv.track("testExecution", res["testExecKey"])
    assert res["summary"] == {"total": 1, "byStatus": {"PASSED": 1}}, res
    cov = lv.call("get_requirement_coverage", issueKey=story)
    assert cov["tests"][0]["lastStatus"] == "PASSED", cov
    assert cov["counts"] == {"total": 1, "byLastStatus": {"PASSED": 1}, "neverRun": 0}
    assert cov["coverageStatus"] == "OK", cov
    log["get_requirement_coverage"] = (
        "ok: link 'Test' (inward 'is tested by', outward 'tests'); linkedIssues(KEY, \"is tested "
        "by\") finds the test; coverageStatus from getCoverableIssues.status: %s before any run, "
        "%s after a PASSED run" % (first_status, cov["coverageStatus"]))

    def found(**crit):
        out = eventually(lambda: lv.call("find_related_tests", **crit),
                         lambda o: any(t["key"] == key for t in o["tests"]))
        rows = [t for t in out["tests"] if t["key"] == key]
        assert len(rows) == 1, out
        return rows[0]

    assert found(labels=[rel_label])["matchedBy"] == ["labels"]
    assert found(terms=[term])["matchedBy"] == ["terms"]
    assert found(issueKeys=[story])["matchedBy"] == ["issueKeys"]
    both = found(labels=[rel_label], terms=[term], issueKeys=[story])
    assert sorted(both["matchedBy"]) == ["issueKeys", "labels", "terms"], both
    assert both["lastStatus"] == "PASSED", both
    log["find_related_tests"] = ("ok: found by label, term and issue key, matchedBy per kind; "
                                 "all three at once -> one row, matchedBy all three, lastStatus")


def test_parity_audit_and_table(results_log):
    if not HAS_XRAY:
        pytest.skip("XRAY_CLIENT_ID/SECRET not set")
    spec = importlib.util.spec_from_file_location("parity_audit", REPO / "dev" / "parity_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert mod.main([]) == 0
    text = buf.getvalue()
    mutations = re.findall(r"^  (\w+)$", text.split("Queries")[0], re.M)
    assert len(mutations) > 20, text[:500]
    doc = (REPO / "docs" / "coverage.md").read_text(encoding="utf-8").split("## API parity")[1]
    rows = {m.group(1) for m in re.finditer(r"^\| `(\w+)` \|", doc, re.M)}
    missing = sorted(set(mutations) - rows)
    assert not missing, "mutations missing from the API parity table: %s" % missing
    stale = sorted(rows - set(mutations))
    assert not stale, "API parity rows for mutations not in the live schema: %s" % stale
    results_log.setdefault("plan2", {})["parity audit"] = (
        "ok: %d mutations, every one has a row" % len(mutations))
