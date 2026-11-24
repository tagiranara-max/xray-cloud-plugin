from __future__ import annotations

import json

import pytest

from conftest import make_context
from xray_cloud import localfiles
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = "projects:\n  PROJ: {}\n"
YAML_FIELDS = """
projects:
  PROJ:
    fields:
      testExecution:
        Release: {id: fixVersions, type: versions, default: ["1.0"]}
"""
YAML_REQ = """
projects:
  PROJ:
    fields:
      testExecution:
        Backend: {id: customfield_12345, type: option, required: true}
"""
RUNS = "GetTestExecutionRuns"


def runs_response(statuses):
    return {"getTestRuns": {"total": len(statuses),
                            "results": [{"status": {"name": s}} for s in statuses]}}


def make(tmp_path, yaml=YAML, statuses=("PASSED", "FAILED", "PASSED"), **kw):
    ctx = make_context(yaml, tmp_path=tmp_path, responses={RUNS: runs_response(statuses)}, **kw)
    ctx.xray.rest_responses[("POST", "import/execution/junit")] = {
        "id": "100", "key": "PROJ-9", "self": "https://x/rest/api/2/issue/100"}
    return ctx


def call(ctx, **args):
    args.setdefault("projectKey", "PROJ")
    res = REGISTRY.call(ctx, "import_execution_results", args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def junit(tmp_path, data=b"<testsuites/>"):
    f = tmp_path / "r.xml"
    f.write_bytes(data)
    return str(f)


def test_junit_file_plain_endpoint_params(tmp_path):
    ctx = make(tmp_path)
    f = junit(tmp_path)
    out, err = call(ctx, format="junit", filePath=f, testEnvironments=["chrome", "linux"],
                    testPlanKey="PROJ-5")
    assert not err
    m, path, kw = ctx.xray.rest_calls[0]
    assert (m, path) == ("POST", "import/execution/junit")
    assert kw["params"] == {"projectKey": "PROJ", "testPlanKey": "PROJ-5",
                            "testEnvironments": "chrome;linux"}
    assert kw["data"] == b"<testsuites/>" and kw["content_type"] == "text/xml"
    assert out["testExecKey"] == "PROJ-9" and out["created"] is True


def test_new_execution_with_fields_uses_multipart_info(tmp_path):
    ctx = make(tmp_path, YAML_FIELDS)
    ctx.xray.rest_responses[("POST", "import/execution/junit/multipart")] = {
        "id": "100", "key": "PROJ-9", "self": "https://x/rest/api/2/issue/100"}
    out, err = call(ctx, format="junit", content="<testsuites/>", summary="Nightly",
                    testPlanKey="PROJ-5", testEnvironments=["chrome"])
    assert not err, out
    m, path, kw = ctx.xray.rest_calls[0]
    assert path == "import/execution/junit/multipart"
    files = kw["multipart"]["files"]
    info = json.loads(files["info"][1])
    assert info["fields"]["fixVersions"] == [{"name": "1.0"}]
    assert info["fields"]["summary"] == "Nightly"
    assert info["fields"]["project"] == {"key": "PROJ"}
    assert info["fields"]["issuetype"] == {"name": "Test Execution"}
    assert info["xrayFields"] == {"testPlanKey": "PROJ-5", "environments": ["chrome"]}
    assert files["results"][1] == b"<testsuites/>"


def test_required_execution_field_missing_refused_before_upload(tmp_path):
    ctx = make(tmp_path, YAML_REQ)
    out, err = call(ctx, format="junit", content="<x/>")
    assert err and "Backend" in json.dumps(out) and "customfield_12345" in json.dumps(out)
    assert ctx.xray.rest_calls == [] and ctx.xray.calls == []


def test_required_field_satisfied_by_fields(tmp_path):
    ctx = make(tmp_path, YAML_REQ)
    ctx.xray.rest_responses[("POST", "import/execution/junit/multipart")] = {
        "id": "100", "key": "PROJ-9", "self": "https://x/rest/api/2/issue/100"}
    out, err = call(ctx, format="junit", content="<x/>", fields={"Backend": "Web"})
    assert not err, out
    info = json.loads(ctx.xray.rest_calls[0][2]["multipart"]["files"]["info"][1])
    assert info["fields"]["customfield_12345"] == {"value": "Web"}
    assert info["fields"]["summary"] == "Imported results (junit)"


def test_existing_execution_never_multipart(tmp_path):
    ctx = make(tmp_path, YAML_REQ)
    out, err = call(ctx, format="junit", content="<x/>", testExecKey="PROJ-9")
    assert not err, out
    m, path, kw = ctx.xray.rest_calls[0]
    assert path == "import/execution/junit" and "multipart" not in kw
    assert kw["params"]["testExecKey"] == "PROJ-9" and out["created"] is False
    out, err = call(ctx, format="junit", content="<x/>", testExecKey="PROJ-9", summary="s")
    assert err and len(ctx.xray.rest_calls) == 1


@pytest.mark.parametrize("args", [{}, {"content": "a", "filePath": "x"}])
def test_content_and_file_both_or_neither_refused(tmp_path, args):
    ctx = make(tmp_path)
    out, err = call(ctx, format="junit", **args)
    assert err and "filePath" in out["error"] and "content" in out["error"]
    assert ctx.xray.rest_calls == []


def test_file_over_limit_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(localfiles, "MAX_FILE_BYTES", 5)
    ctx = make(tmp_path)
    out, err = call(ctx, format="junit", filePath=junit(tmp_path, b"0123456789"))
    assert err and "too large" in out["error"] and ctx.xray.rest_calls == []
    out, err = call(ctx, format="junit", content="0123456789")
    assert err and "too large" in out["error"] and ctx.xray.rest_calls == []


def test_non_utf8_xml_sent_untouched(tmp_path):
    raw = "<?xml version='1.0' encoding='latin-1'?><a>\xe9</a>".encode("latin-1")
    ctx = make(tmp_path)
    out, err = call(ctx, format="junit", filePath=junit(tmp_path, raw))
    assert not err, out
    assert ctx.xray.rest_calls[0][2]["data"] == raw


def test_xray_json_plain_endpoint_bytes_untouched_when_project_set(tmp_path):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution")] = {"id": "7", "key": "PROJ-7"}
    raw = '{"info": {"project": "PROJ"},  "tests": [{"testKey": "PROJ-1", "status": "PASSED"}]}'
    out, err = call(ctx, format="xray", content=raw)
    assert not err, out
    m, p, kw = ctx.xray.rest_calls[-1]
    assert p == "import/execution" and kw["data"] == raw.encode("utf-8")
    assert kw["content_type"] == "application/json"
    assert "params" not in kw or not kw["params"]
    assert out["testExecKey"] == "PROJ-7" and out["created"] is True


def test_xray_json_without_project_gets_info_project(tmp_path):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution")] = {"id": "7", "key": "PROJ-7"}
    out, err = call(ctx, format="xray", content='{"tests": []}')
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[-1][2]["data"])
    assert body == {"tests": [], "info": {"project": "PROJ"}}
    out, err = call(ctx, format="xray", content='{"info": {"summary": "s"}, "tests": []}')
    body = json.loads(ctx.xray.rest_calls[-1][2]["data"])
    assert body["info"] == {"summary": "s", "project": "PROJ"}


@pytest.mark.parametrize("report,bad", [
    ({"testExecutionKey": "OTHER-5", "tests": []}, "OTHER-5"),
    ({"info": {"testPlanKey": "OTHER-6"}, "tests": []}, "OTHER-6"),
    ({"info": {"project": "OTHER"}, "tests": []}, "OTHER"),
    ({"tests": [{"testKey": "PROJ-1"}, {"testKey": "OTHER-7", "status": "PASSED"}]}, "OTHER-7"),
    ({"tests": [{"testInfo": {"projectKey": "OTHER", "summary": "x"}, "status": "PASSED"}]},
     "OTHER"),
    ([], "object"),
    ({"tests": {}}, "tests"),
    ({"info": "x", "tests": []}, "info"),
])
def test_xray_json_keys_outside_project_refused_before_any_call(tmp_path, report, bad):
    ctx = make(tmp_path)
    out, err = call(ctx, format="xray", content=json.dumps(report))
    assert err and bad in json.dumps(out), out
    assert ctx.xray.rest_calls == [] and ctx.xray.calls == []


def test_xray_json_report_execution_key_means_existing_execution(tmp_path):
    ctx = make(tmp_path, YAML_REQ)  # required execution fields would force multipart for a new one
    ctx.xray.rest_responses[("POST", "import/execution")] = {"id": "9", "key": "PROJ-9"}
    raw = '{"testExecutionKey": "PROJ-9", "info": {"project": "PROJ"}, "tests": []}'
    out, err = call(ctx, format="xray", content=raw)
    assert not err, out
    m, p, kw = ctx.xray.rest_calls[0]
    assert p == "import/execution" and "multipart" not in kw and kw["data"] == raw.encode()
    assert out["testExecKey"] == "PROJ-9" and out["created"] is False
    out, err = call(ctx, format="xray", content=raw, summary="s")
    assert err and len(ctx.xray.rest_calls) == 1
    out, err = call(ctx, format="xray", content=raw, testExecKey="PROJ-9")
    assert not err and out["created"] is False


TAGGED = json.dumps([{"name": "F", "elements": [
    {"keyword": "Scenario", "name": "s", "tags": [{"name": "@TEST_PROJ-1"}], "steps": []}]}])
TAGGED_BEHAVE = json.dumps([{"name": "F", "elements": [
    {"keyword": "Scenario", "name": "s", "tags": ["TEST_PROJ-1"], "steps": []}]}])


@pytest.mark.parametrize("fmt", ["cucumber", "behave"])
def test_cucumber_behave_new_execution_always_multipart_with_project(tmp_path, fmt):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution/%s/multipart" % fmt)] = {
        "id": "7", "key": "PROJ-7"}
    content = TAGGED if fmt == "cucumber" else TAGGED_BEHAVE
    out, err = call(ctx, format=fmt, content=content)
    assert not err, out
    m, p, kw = ctx.xray.rest_calls[0]
    assert p == "import/execution/%s/multipart" % fmt
    files = kw["multipart"]["files"]
    info = json.loads(files["info"][1])
    assert info["fields"]["project"] == {"key": "PROJ"}
    assert info["fields"]["issuetype"] == {"name": "Test Execution"}
    assert "xrayFields" not in info
    assert files["results"][1] == content.encode()
    assert out["testExecKey"] == "PROJ-7" and out["created"] is True


@pytest.mark.parametrize("envs", [[""], ["  "], ["a;b"], ["ok", 5]])
def test_bad_environment_names_refused_before_any_call(tmp_path, envs):
    ctx = make(tmp_path)
    out, err = call(ctx, format="junit", content="<x/>", testEnvironments=envs)
    assert err and "nvironment" in out["error"], out
    assert ctx.xray.rest_calls == [] and ctx.xray.calls == []


def test_empty_input_refused_before_any_call(tmp_path):
    ctx = make(tmp_path)
    out, err = call(ctx, format="junit", content="")
    assert err and "empty" in out["error"]
    out, err = call(ctx, format="junit", filePath=junit(tmp_path, b""))
    assert err and "empty" in out["error"]
    assert ctx.xray.rest_calls == [] and ctx.xray.calls == []


def test_json_format_new_execution_with_envs_goes_multipart(tmp_path):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution/cucumber/multipart")] = {
        "id": "7", "key": "PROJ-7"}
    out, err = call(ctx, format="cucumber", content=TAGGED, testEnvironments=["a"],
                    testPlanKey="PROJ-5")
    assert not err, out
    m, path, kw = ctx.xray.rest_calls[0]
    assert path == "import/execution/cucumber/multipart"
    info = json.loads(kw["multipart"]["files"]["info"][1])
    assert info["xrayFields"] == {"testPlanKey": "PROJ-5", "environments": ["a"]}
    assert kw["multipart"]["files"]["results"][1] == TAGGED.encode()


def _step(status, name="it works", error=None):
    res = {"status": status, "duration": 1}
    if error:
        res["error_message"] = error
    return {"keyword": "Then ", "name": name, "result": res}


def _scenario(tags, *steps, kind="scenario", outline=False, el_id=None):
    el = {"type": kind, "keyword": "Scenario Outline" if outline else "Scenario",
          "name": "S %s" % tags, "tags": [{"name": t} for t in tags], "steps": list(steps)}
    if el_id:
        el["id"] = el_id
    return el


def cucumber_report(*elements):
    return json.dumps([{"name": "F", "uri": "f.feature", "elements": [
        {"type": "background", "keyword": "Background", "steps": [_step("passed")]}]
        + list(elements)}])


def test_cucumber_into_existing_execution_sent_as_xray_json(tmp_path):
    """Xray Cloud's Cucumber endpoints always create a new execution (verified live), so
    results for an existing one are converted to Xray JSON with testExecutionKey."""
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution")] = {"id": "9", "key": "PROJ-9"}
    report = cucumber_report(
        _scenario(["@smoke", "@TEST_PROJ-1"], _step("passed"), _step("passed")),
        _scenario(["@TEST_PROJ-2"], _step("passed"), _step("failed", "x", "boom")),
        _scenario(["@TEST_PROJ-3"], _step("passed"), outline=True),     # outline row 1
        _scenario(["@TEST_PROJ-3"], _step("undefined"), outline=True),  # outline row 2
        _scenario(["@TEST_PROJ-4"], _step("skipped")))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9",
                    testEnvironments=["chrome"])
    assert not err, out
    m, path, kw = ctx.xray.rest_calls[0]
    assert (m, path) == ("POST", "import/execution") and "params" not in kw
    assert kw["content_type"] == "application/json"
    body = json.loads(kw["data"])
    assert body["testExecutionKey"] == "PROJ-9"
    assert body["info"] == {"testEnvironments": ["chrome"]}
    assert body["tests"] == [
        {"testKey": "PROJ-1", "status": "PASSED"},
        {"testKey": "PROJ-2", "status": "FAILED", "comment": "Then x: boom"},
        {"testKey": "PROJ-3", "status": "FAILED", "comment": "Then it works: undefined",
         "examples": ["PASSED", "FAILED"]},
        {"testKey": "PROJ-4", "status": "TODO"}]
    assert out["testExecKey"] == "PROJ-9" and out["created"] is False
    assert "converted from cucumber" in out["sentAs"]


def test_cucumber_scenario_with_two_test_tags_is_ambiguous(tmp_path):
    ctx = make(tmp_path)
    report = cucumber_report(_scenario(["@TEST_PROJ-1"], _step("passed")),
                             _scenario(["@TEST_PROJ-1", "@TEST_PROJ-2"], _step("passed")))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9")
    assert not err and out["status"] == "needs_answer", out
    assert out["ambiguous"] == [{"name": "S ['@TEST_PROJ-1', '@TEST_PROJ-2']",
                                 "candidates": ["PROJ-1", "PROJ-2"]}]
    assert ctx.xray.rest_calls == [] and ctx.xray.calls == []


def test_cucumber_into_existing_execution_refuses_other_project_and_bad_json(tmp_path):
    ctx = make(tmp_path)
    out, err = call(ctx, format="cucumber", testExecKey="PROJ-9",
                    content=cucumber_report(_scenario(["@TEST_OTHER-1"], _step("passed"))))
    assert err and "OTHER-1" in json.dumps(out)
    out, err = call(ctx, format="cucumber", content="{not json", testExecKey="PROJ-9")
    assert err and "JSON" in out["error"]
    out, err = call(ctx, format="cucumber", content="[]", testExecKey="PROJ-9")
    assert err and "no scenarios" in out["error"]
    assert ctx.xray.rest_calls == []


def converted(ctx, report):
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9")
    assert not err, out
    return json.loads(ctx.xray.rest_calls[-1][2]["data"])["tests"]


def existing_ctx(tmp_path):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution")] = {"id": "9", "key": "PROJ-9"}
    return ctx


def test_cucumber_failed_background_fails_the_next_scenario(tmp_path):
    """Cucumber JSON emits the Background as its own element before each scenario; when a
    Background step fails, the scenario's steps are skipped. That run is FAILED, not TODO."""
    bg = lambda status, err=None: {"type": "background", "keyword": "Background",
                                   "steps": [_step(status, "the app is running", err)]}
    report = json.dumps([{"name": "F", "elements": [
        bg("failed", "no app"), _scenario(["@TEST_PROJ-1"], _step("skipped")),
        bg("passed"), _scenario(["@TEST_PROJ-2"], _step("passed"))]}])
    assert converted(existing_ctx(tmp_path), report) == [
        {"testKey": "PROJ-1", "status": "FAILED", "comment": "Then the app is running: no app"},
        {"testKey": "PROJ-2", "status": "PASSED"}]


def test_cucumber_repeated_plain_scenario_keeps_worst_status_without_examples(tmp_path):
    report = cucumber_report(_scenario(["@TEST_PROJ-1"], _step("passed")),
                             _scenario(["@TEST_PROJ-1"], _step("skipped")),
                             _scenario(["@TEST_PROJ-2"], _step("failed", "x", "e")),
                             _scenario(["@TEST_PROJ-2"], _step("passed")))
    assert converted(existing_ctx(tmp_path), report) == [
        {"testKey": "PROJ-1", "status": "TODO"},
        {"testKey": "PROJ-2", "status": "FAILED", "comment": "Then x: e"}]


def test_cucumber_outline_rows_recognised_by_example_row_id(tmp_path):
    report = cucumber_report(
        _scenario(["@TEST_PROJ-1"], _step("passed"), el_id="login;failed-login;;2"),
        _scenario(["@TEST_PROJ-1"], _step("passed"), el_id="login;failed-login;rows;3"))
    assert converted(existing_ctx(tmp_path), report) == [
        {"testKey": "PROJ-1", "status": "PASSED", "examples": ["PASSED", "PASSED"]}]


def test_cucumber_failing_hooks_fail_the_test(tmp_path):
    step_hook = dict(_step("passed"), after=[{"result": {"status": "failed",
                                                         "error_message": "screenshot"}}])
    scen_hook = dict(_scenario(["@TEST_PROJ-2"], _step("passed")),
                     before=[{"result": {"status": "failed", "error_message": "db down"}}])
    report = cucumber_report(_scenario(["@TEST_PROJ-1"], step_hook), scen_hook)
    assert converted(existing_ctx(tmp_path), report) == [
        {"testKey": "PROJ-1", "status": "FAILED",
         "comment": "After step hook (Then it works): screenshot"},
        {"testKey": "PROJ-2", "status": "FAILED", "comment": "Before hook: db down"}]


def test_xray_json_into_existing_execution_sets_testexecutionkey(tmp_path):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution")] = {"id": "9", "key": "PROJ-9"}
    report = json.dumps({"info": {"summary": "s"}, "tests": [
        {"testKey": "PROJ-1", "status": "PASSED"}]})
    out, err = call(ctx, format="xray", content=report, testExecKey="PROJ-9",
                    testPlanKey="PROJ-5")
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert body["testExecutionKey"] == "PROJ-9"
    assert body["info"] == {"summary": "s", "testPlanKey": "PROJ-5", "project": "PROJ"}
    assert "sentAs" not in out
    clash = json.dumps({"testExecutionKey": "PROJ-8", "tests": []})
    out, err = call(ctx, format="xray", content=clash, testExecKey="PROJ-9")
    assert err and "PROJ-8" in out["error"] and len(ctx.xray.rest_calls) == 1


def test_json_format_new_execution_with_fields_multipart(tmp_path):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution/cucumber/multipart")] = {
        "id": "7", "key": "PROJ-7", "self": "https://x/rest/api/2/issue/7"}
    out, err = call(ctx, format="cucumber", content=TAGGED, summary="S")
    assert not err, out
    files = ctx.xray.rest_calls[0][2]["multipart"]["files"]
    assert "results" in files and "result" not in files and "info" in files


def test_summary_counts_by_status(tmp_path):
    ctx = make(tmp_path, statuses=["PASSED", "FAILED", "PASSED", "TODO"])
    out, _ = call(ctx, format="junit", content="<x/>", detail=True)
    assert out["summary"] == {"total": 4, "byStatus": {"PASSED": 2, "FAILED": 1, "TODO": 1}}
    assert out["response"] == {"id": "100", "key": "PROJ-9", "self": "https://x/rest/api/2/issue/100"}
    assert ctx.xray.calls_of(RUNS)[0]["testExecIssueIds"] == ["100"]
    out, _ = call(ctx, format="junit", content="<x/>")
    assert "response" not in out


def test_read_only_blocks(tmp_path):
    ctx = make(tmp_path, read_only=True)
    out, err = call(ctx, format="junit", content="<x/>")
    assert err and ctx.xray.rest_calls == []


def test_exec_key_other_project_refused(tmp_path):
    ctx = make(tmp_path)
    out, err = call(ctx, format="junit", content="<x/>", testExecKey="OTHER-1")
    assert err and ctx.xray.rest_calls == []
    out, err = call(ctx, format="junit", content="<x/>", testPlanKey="OTHER-1")
    assert err and ctx.xray.rest_calls == []


def test_bad_format_refused(tmp_path):
    ctx = make(tmp_path)
    out, err = call(ctx, format="csv", content="x")
    assert err and ctx.xray.rest_calls == []


def test_server_dc_response_shape_still_read(tmp_path):
    """Xray Server/DC answers {"testExecIssue": {...}}; Cloud answers {"id", "key", "self"}."""
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution/junit")] = {
        "testExecIssue": {"id": "100", "key": "PROJ-9"}}
    out, err = call(ctx, format="junit", content="<x/>")
    assert not err and out["testExecKey"] == "PROJ-9" and out["summary"]["total"] == 3
    assert "warnings" not in out


# ---- matching results to tests, never creating tests without consent -----------------------

SUMMARIES, DEFINITIONS, KEYS = "ResultMatchSummaries", "ResultMatchDefinitions", "ResultMatchKeys"


def rows_by(rows):
    """getTests response for (key, summary, definition) rows (one page)."""
    return {"getTests": {"total": len(rows), "results": [
        {"issueId": str(100 + i), "unstructured": d, "jira": {"key": k, "summary": s}}
        for i, (k, s, d) in enumerate(rows)]}}


def run_tests_response(keys, statuses=None):
    statuses = statuses or ["PASSED"] * len(keys)
    return {"getTestRuns": {"total": len(keys), "results": [
        {"status": {"name": s}, "test": {"jira": {"key": k}}} for k, s in zip(keys, statuses)]}}


def match_ctx(tmp_path, summaries=(), definitions=(), yaml=YAML, runs=None, before=None):
    """A context whose summary/definition lookups answer with these rows; `runs` are the test
    keys of the execution after the import, `before` those already in it."""
    ctx = make(tmp_path, yaml)
    ctx.xray.responses[SUMMARIES] = rows_by(list(summaries))
    ctx.xray.responses[DEFINITIONS] = rows_by(list(definitions))
    state = {"uploaded": False}
    for path in ("import/execution", "import/execution/multipart",
                 "import/execution/cucumber/multipart", "import/execution/behave/multipart",
                 "import/execution/junit", "import/execution/robot"):
        def answer(kw, path=path):
            state["uploaded"] = True
            return {"id": "9", "key": "PROJ-9"}
        ctx.xray.rest_responses[("POST", path)] = answer
    # By default every key asked about is an existing Test.
    ctx.xray.responses[KEYS] = lambda v: rows_by(
        [(k, "Test " + k, None) for k in v["jql"][len("issue in ("):-1].split(", ")])
    ctx.xray.responses["ResultExecId"] = {"getTestExecutions": {"results": [{"issueId": "9"}]}}
    ctx.xray.responses[RUNS] = lambda v: run_tests_response(
        list(runs or []) if state["uploaded"] else list(before or []))
    return ctx


def untagged(name, *steps, **kw):
    el = _scenario([], *(steps or (_step("passed"),)), **kw)
    el["name"] = name
    return el


def test_untagged_cucumber_into_existing_execution_matched_by_summary(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login works", None),
                                         ("PROJ-2", "Logout works", None)])
    report = cucumber_report(untagged("Login works"),
                             untagged("  Logout works ", _step("failed", "x", "boom")))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9")
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert body["testExecutionKey"] == "PROJ-9"
    assert body["tests"] == [{"testKey": "PROJ-1", "status": "PASSED"},
                             {"testKey": "PROJ-2", "status": "FAILED",
                              "comment": "Then x: boom"}]
    assert "createdTests" not in out


def test_unmatched_scenarios_need_an_answer_and_nothing_is_uploaded(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login works", None),
                                         ("PROJ-2", "Twin", None), ("PROJ-3", "Twin", None)])
    report = cucumber_report(untagged("Login works"), untagged("Brand new"), untagged("Twin"),
                             _scenario(["@TEST_PROJ-4"], _step("passed")))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9")
    assert not err, out
    assert out["status"] == "needs_answer" and ctx.xray.rest_calls == []
    assert out["matched"] == 2
    assert out["ambiguous"] == [{"name": "Twin", "candidates": ["PROJ-2", "PROJ-3"]}]
    assert out["missing"] == ["Brand new"]
    assert out["ambiguousCount"] == 1 and out["missingCount"] == 1
    assert sorted(q["id"] for q in out["questions"]) == ["allowCreateTests", "testKeys"]


def test_needs_answer_lists_are_capped(tmp_path):
    ctx = match_ctx(tmp_path)
    report = cucumber_report(*[untagged("new %d" % i) for i in range(60)])
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9")
    assert out["status"] == "needs_answer"
    assert len(out["missing"]) == 50 and out["missingCount"] == 60
    assert [q["id"] for q in out["questions"]] == ["allowCreateTests"]


def test_test_keys_resolve_ambiguous_and_missing(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-2", "Twin", None), ("PROJ-3", "Twin", None)])
    report = cucumber_report(untagged("Twin"), untagged("Brand new"))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9",
                    testKeys={"Twin": "PROJ-3", "Brand new": "PROJ-7"})
    assert not err and "status" not in out, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert [t["testKey"] for t in body["tests"]] == ["PROJ-3", "PROJ-7"]


@pytest.mark.parametrize("keys,needle", [({"Nope": "PROJ-1"}, "Nope"),
                                         ({"Twin": "OTHER-1"}, "OTHER-1"),
                                         ({"Twin": "nokey"}, "nokey"),
                                         ("x", "testKeys")])
def test_bad_test_keys_refused_before_any_call(tmp_path, keys, needle):
    ctx = match_ctx(tmp_path)
    out, err = call(ctx, format="cucumber", content=cucumber_report(untagged("Twin")),
                    testExecKey="PROJ-9", testKeys=keys)
    assert err and needle in json.dumps(out), out
    assert ctx.xray.rest_calls == []


def test_allow_create_sends_test_info_and_lists_created_tests(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login works", None)],
                    before=["PROJ-5"], runs=["PROJ-5", "PROJ-1", "PROJ-20"])
    new = untagged("Brand new", _step("passed", "I log in"), _step("passed", "I see it"))
    report = cucumber_report(untagged("Login works"), new)
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9",
                    allowCreateTests=True)
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert body["tests"] == [
        {"testKey": "PROJ-1", "status": "PASSED"},
        {"testInfo": {"projectKey": "PROJ", "summary": "Brand new", "type": "Cucumber",
                      "scenario": "Then I log in\nThen I see it"}, "status": "PASSED"}]
    assert out["createdTests"] == ["PROJ-20"]
    assert "AI label" in out["note"]


def test_skip_missing_leaves_unmatched_results_out(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login works", None)])
    report = cucumber_report(untagged("Login works"), untagged("Brand new"))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9",
                    skipMissing=True)
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert body["tests"] == [{"testKey": "PROJ-1", "status": "PASSED"}]
    assert out["skipped"] == ["Brand new"]
    out, err = call(ctx, format="cucumber", content=cucumber_report(untagged("Brand new")),
                    testExecKey="PROJ-9", skipMissing=True)
    assert err and "nothing" in out["error"].lower() and len(ctx.xray.rest_calls) == 1
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9",
                    skipMissing=True, allowCreateTests=True)
    assert err and len(ctx.xray.rest_calls) == 1


def test_test_key_tag_setting_names_the_test(tmp_path):
    yaml = "projects:\n  PROJ: {testKeyTag: issue}\n"
    ctx = match_ctx(tmp_path, yaml=yaml)
    report = cucumber_report(_scenario(["@issue=PROJ-4"], _step("passed")),
                             _scenario(["@issue:PROJ-5"], _step("passed")))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9")
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert [t["testKey"] for t in body["tests"]] == ["PROJ-4", "PROJ-5"]
    assert ctx.xray.calls_of(SUMMARIES) == []  # no lookup needed


def test_new_execution_tagged_cucumber_bytes_untouched(tmp_path):
    ctx = match_ctx(tmp_path)
    report = cucumber_report(_scenario(["@TEST_PROJ-1"], _step("passed")))
    out, err = call(ctx, format="cucumber", content=report)
    assert not err, out
    m, p, kw = ctx.xray.rest_calls[0]
    assert p == "import/execution/cucumber/multipart"
    assert kw["multipart"]["files"]["results"][1] == report.encode()
    assert ctx.xray.calls_of(SUMMARIES) == []


def test_new_execution_cucumber_matched_by_summary_gets_test_tags(tmp_path):
    """Xray's Cucumber import maps scenarios by @TEST_ tag only (verified live), so a
    scenario matched by summary is sent with that tag; step details stay native."""
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login works", None)])
    report = cucumber_report(untagged("Login works"),
                             _scenario(["@TEST_PROJ-2", "@TEST_PROJ-3"], _step("passed")))
    out, err = call(ctx, format="cucumber", content=report,
                    testKeys={"S ['@TEST_PROJ-2', '@TEST_PROJ-3']": "PROJ-3"})
    assert not err, out
    m, p, kw = ctx.xray.rest_calls[0]
    assert p == "import/execution/cucumber/multipart"
    sent = json.loads(kw["multipart"]["files"]["results"][1])
    tags = [[t["name"] for t in el.get("tags", [])] for el in sent[0]["elements"][1:]]
    assert tags == [["@TEST_PROJ-1"], ["@TEST_PROJ-3"]]
    assert "sentAs" not in out


def test_new_execution_cucumber_with_created_tests_goes_as_xray_json(tmp_path):
    ctx = match_ctx(tmp_path, runs=["PROJ-30"])
    out, err = call(ctx, format="cucumber", content=cucumber_report(untagged("Brand new")),
                    allowCreateTests=True, testEnvironments=["chrome"])
    assert not err, out
    m, p, kw = ctx.xray.rest_calls[0]
    assert p == "import/execution/multipart"
    files = kw["multipart"]["files"]
    assert json.loads(files["info"][1])["xrayFields"] == {"environments": ["chrome"]}
    body = json.loads(files["results"][1])
    assert body["tests"][0]["testInfo"]["summary"] == "Brand new"
    assert out["createdTests"] == ["PROJ-30"] and "converted from cucumber" in out["sentAs"]


def test_behave_matched_by_summary_into_existing_execution(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login", None)])
    report = json.dumps([{"keyword": "Feature", "name": "F", "tags": [], "elements": [
        {"keyword": "Scenario Outline", "name": "Login -- @1.1 Users", "tags": [], "steps": [
            {"keyword": "Given", "name": "a", "result": {"status": "passed"}}]},
        {"keyword": "Scenario Outline", "name": "Login -- @1.2 Users", "tags": [], "steps": [
            {"keyword": "Given", "name": "a", "result": {"status": "failed",
                                                         "error_message": ["bad", "x"]}}]}]}])
    out, err = call(ctx, format="behave", content=report, testExecKey="PROJ-9")
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert body["tests"] == [{"testKey": "PROJ-1", "status": "FAILED",
                              "comment": "Given a: bad\nx", "examples": ["PASSED", "FAILED"]}]
    assert "converted from behave" in out["sentAs"]


def test_behave_new_execution_gets_plain_test_tags(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login", None)])
    report = json.dumps([{"keyword": "Feature", "name": "F", "elements": [
        {"keyword": "Scenario", "name": "Login", "tags": ["smoke"], "steps": []}]}])
    out, err = call(ctx, format="behave", content=report)
    assert not err, out
    sent = json.loads(ctx.xray.rest_calls[0][2]["multipart"]["files"]["results"][1])
    assert sent[0]["elements"][0]["tags"] == ["smoke", "TEST_PROJ-1"]


JUNIT_CASES = ('<testsuites><testsuite name="s"><testcase classname="a.B" name="known"/>'
               '<testcase classname="a.B" name="fresh"/></testsuite></testsuites>')


def test_junit_unmatched_case_needs_an_answer(tmp_path):
    ctx = match_ctx(tmp_path, definitions=[("PROJ-1", "known", "a.B.known")])
    out, err = call(ctx, format="junit", content=JUNIT_CASES)
    assert not err, out
    assert out["status"] == "needs_answer" and ctx.xray.rest_calls == []
    assert out["matched"] == 1 and out["missing"] == ["a.B.fresh"]
    assert "Generic" in out["questions"][0]["question"]


def test_junit_allow_create_sends_bytes_untouched_and_lists_created(tmp_path):
    ctx = match_ctx(tmp_path, definitions=[("PROJ-1", "known", "a.B.known")],
                    runs=["PROJ-1", "PROJ-40"])
    out, err = call(ctx, format="junit", content=JUNIT_CASES, allowCreateTests=True)
    assert not err, out
    assert ctx.xray.rest_calls[0][2]["data"] == JUNIT_CASES.encode()
    assert out["createdTests"] == ["PROJ-40"]


def test_junit_all_matched_needs_no_answer_and_no_created_list(tmp_path):
    ctx = match_ctx(tmp_path, definitions=[("PROJ-1", "known", "a.B.known"),
                                           ("PROJ-2", "fresh", "a.B.fresh")])
    out, err = call(ctx, format="junit", content=JUNIT_CASES)
    assert not err and "status" not in out and "createdTests" not in out, out
    assert ctx.xray.rest_calls[0][2]["data"] == JUNIT_CASES.encode()


def test_junit_test_keys_injected_as_test_key_property_and_skip_removes(tmp_path):
    import xml.etree.ElementTree as ET
    ctx = match_ctx(tmp_path)
    out, err = call(ctx, format="junit", content=JUNIT_CASES,
                    testKeys={"a.B.known": "PROJ-7"}, skipMissing=True)
    assert not err, out
    root = ET.fromstring(ctx.xray.rest_calls[0][2]["data"])
    cases = root.findall(".//testcase")
    assert [c.get("name") for c in cases] == ["known"]
    assert cases[0].find("properties/property").attrib == {"name": "test_key",
                                                            "value": "PROJ-7"}
    assert out["skipped"] == ["a.B.fresh"]


def test_junit_key_property_of_other_project_refused(tmp_path):
    ctx = match_ctx(tmp_path)
    xml = ('<testsuite><testcase name="x"><properties><property name="test_key" '
           'value="OTHER-1"/></properties></testcase></testsuite>')
    out, err = call(ctx, format="junit", content=xml)
    assert err and "OTHER-1" in json.dumps(out) and ctx.xray.rest_calls == []


def test_junit_keyed_cases_need_no_lookup(tmp_path):
    ctx = match_ctx(tmp_path)
    xml = ('<testsuite><testcase name="x"><properties><property name="test_key" '
           'value="PROJ-1"/></properties></testcase></testsuite>')
    out, err = call(ctx, format="junit", content=xml)
    assert not err and "status" not in out, out
    assert ctx.xray.calls_of(DEFINITIONS) == []


def test_robot_key_tag_that_is_not_a_test_does_not_count(tmp_path):
    ctx = match_ctx(tmp_path)
    ctx.xray.responses[KEYS] = rows_by([])  # PROJ-3 is a story, not a test
    xml = ('<robot><suite name="S"><test name="T"><tags><tag>PROJ-3</tag><tag>ISO-8601</tag>'
           '<tag>OTHER-4</tag></tags></test></suite></robot>')
    out, err = call(ctx, format="robot", content=xml)
    assert out["status"] == "needs_answer" and out["missing"] == ["S.T"], out


def test_robot_tag_naming_a_test_of_another_project_refused(tmp_path):
    ctx = match_ctx(tmp_path)
    ctx.xray.responses[KEYS] = rows_by([("OTHER-4", "T", None)])
    xml = '<robot><suite name="S"><test name="T"><tags><tag>OTHER-4</tag></tags></test></suite></robot>'
    out, err = call(ctx, format="robot", content=xml)
    assert err and "OTHER-4" in json.dumps(out) and ctx.xray.rest_calls == []


ROBOT = ('<robot><suite name="S"><test name="Login"><tags><tag>PROJ-5</tag>'
         '<tag>PROJ-77</tag><tag>smoke</tag></tags></test></suite></robot>')


def robot_tests(ctx):
    """(name, tags) of each test in the uploaded Robot copy."""
    import xml.etree.ElementTree as ET
    return [(t.get("name"), [g.text for g in t.iter("tag")])
            for t in ET.fromstring(ctx.xray.rest_calls[0][2]["data"]).iter("test")]


def test_robot_key_tag_match_uploads_the_test_summary_as_name(tmp_path):
    """Xray renames a matched test to the Robot test's name; the uploaded copy carries the
    test's current summary instead, so nothing changes (verified live)."""
    ctx = match_ctx(tmp_path)  # PROJ-77 is a requirement (not a test)
    ctx.xray.responses[KEYS] = rows_by([("PROJ-5", "Old name", None)])
    out, err = call(ctx, format="robot", content=ROBOT)
    assert not err and "status" not in out, out
    assert robot_tests(ctx) == [("Old name", ["PROJ-5", "PROJ-77"])]
    assert "allowRenameTests" not in json.dumps(out)


def test_robot_same_name_and_key_tag_only_sent_unchanged(tmp_path):
    ctx = match_ctx(tmp_path)
    ctx.xray.responses[KEYS] = rows_by([("PROJ-5", "Login", None)])
    xml = '<robot><suite name="S"><test name="Login"><tags><tag>PROJ-5</tag></tags></test></suite></robot>'
    out, err = call(ctx, format="robot", content=xml)
    assert not err and ctx.xray.rest_calls[0][2]["data"] == xml.encode(), out


def test_robot_test_keys_injects_key_tag_and_summary_name(tmp_path):
    ctx = match_ctx(tmp_path)
    ctx.xray.responses[KEYS] = lambda v: rows_by(
        [r for r in [("PROJ-5", "Login", None), ("PROJ-6", "Other", None)]
         if r[0] in v["jql"]])
    out, err = call(ctx, format="robot", content=ROBOT, testKeys={"S.Login": "PROJ-6"})
    assert not err and "status" not in out, out
    assert robot_tests(ctx) == [("Other", ["PROJ-77", "PROJ-6"])]


def test_matched_junit_case_loses_properties_that_would_update_the_test(tmp_path):
    import xml.etree.ElementTree as ET
    ctx = match_ctx(tmp_path, definitions=[("PROJ-1", "c", "a.B.c")])
    xml = ('<testsuite><testcase classname="a.B" name="c"><properties>'
           '<property name="test_summary" value="New"/><property name="tags" value="x"/>'
           '<property name="test_description" value="d"/><property name="requirements" '
           'value="PROJ-9"/></properties></testcase></testsuite>')
    out, err = call(ctx, format="junit", content=xml)
    assert not err, out
    props = [p.get("name") for p in ET.fromstring(ctx.xray.rest_calls[0][2]["data"]).iter("property")]
    assert props == ["requirements"]
    assert any("test_summary" in w for w in out["warnings"]), out


def test_created_junit_case_keeps_its_properties(tmp_path):
    ctx = match_ctx(tmp_path)
    xml = ('<testsuite><testcase classname="a.B" name="c"><properties>'
           '<property name="test_summary" value="New"/></properties></testcase></testsuite>')
    out, err = call(ctx, format="junit", content=xml, allowCreateTests=True)
    assert not err and ctx.xray.rest_calls[0][2]["data"] == xml.encode(), out


def test_ambiguous_candidates_capped(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-%d" % i, "Twin", None) for i in range(1, 15)])
    out, err = call(ctx, format="cucumber", content=cucumber_report(untagged("Twin")),
                    testExecKey="PROJ-9")
    amb = out["ambiguous"][0]
    assert len(amb["candidates"]) == 10 and amb["candidatesCount"] == 14, out


def test_same_scenario_name_in_two_features_resolved_separately(tmp_path):
    ctx = match_ctx(tmp_path, summaries=[("PROJ-1", "Login", None)])
    report = json.dumps([{"name": "A", "elements": [untagged("Login")]},
                         {"name": "B", "elements": [untagged("Login")]}])
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9",
                    testKeys={"B: Login": "PROJ-2"})
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert [t["testKey"] for t in body["tests"]] == ["PROJ-1", "PROJ-2"]


def test_xray_json_test_key_entries_lose_test_info(tmp_path):
    """Xray overwrites a test's summary and definition from testInfo even when testKey is
    given (verified live), so testInfo is dropped."""
    ctx = match_ctx(tmp_path)
    report = {"tests": [{"testKey": "PROJ-2", "status": "PASSED",
                         "testInfo": {"summary": "x", "type": "Generic", "definition": "d"}}]}
    out, err = call(ctx, format="xray", content=json.dumps(report))
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    assert body["tests"] == [{"testKey": "PROJ-2", "status": "PASSED"}]
    assert out["warnings"], out


def test_xray_json_colliding_summaries_named_apart(tmp_path):
    ctx = match_ctx(tmp_path)
    report = {"tests": [
        {"testInfo": {"summary": "Same", "type": "Generic", "definition": "a.b"}, "status": "PASSED"},
        {"testInfo": {"summary": "Same", "type": "Generic", "definition": "a.c"}, "status": "PASSED"}]}
    out, err = call(ctx, format="xray", content=json.dumps(report))
    assert out["missing"] == ["Same (a.b)", "Same (a.c)"], out


def test_invalid_xml_refused_before_upload(tmp_path):
    ctx = match_ctx(tmp_path)
    out, err = call(ctx, format="junit", content="<testsuite>")
    assert err and "XML" in out["error"] and ctx.xray.rest_calls == []


def test_xray_json_test_info_entries_go_through_the_gate(tmp_path):
    ctx = match_ctx(tmp_path, definitions=[("PROJ-1", "x", "a.B.c")])
    report = {"tests": [
        {"testKey": "PROJ-2", "status": "PASSED"},
        {"testInfo": {"summary": "gen", "type": "Generic", "definition": "a.B.c"},
         "status": "PASSED"},
        {"testInfo": {"summary": "New one", "type": "Cucumber", "scenario": "Given x"},
         "status": "FAILED"}]}
    out, err = call(ctx, format="xray", content=json.dumps(report))
    assert out["status"] == "needs_answer" and out["missing"] == ["New one"], out
    assert out["matched"] == 2 and ctx.xray.rest_calls == []
    out, err = call(ctx, format="xray", content=json.dumps(report),
                    testKeys={"New one": "PROJ-8"})
    assert not err, out
    body = json.loads(ctx.xray.rest_calls[0][2]["data"])
    # matched by definition: sent by key, so Xray does not overwrite PROJ-1's summary
    assert body["tests"][1] == {"testKey": "PROJ-1", "status": "PASSED"}
    assert body["tests"][2] == {"testKey": "PROJ-8", "status": "FAILED"}


ROBOT_DEF = ('<robot><suite name="S"><test name="Login"><tags><tag>smoke</tag></tags></test>'
             '<test name="New"><tags><tag>smoke</tag></tags></test></suite></robot>')


def test_robot_definition_match_gets_key_tag_and_summary_name(tmp_path):
    """A definition match is pinned with the test's key tag, so renaming the copy keeps the
    match; plain tags are dropped from matched tests (they would become labels)."""
    ctx = match_ctx(tmp_path, definitions=[("PROJ-5", "Old name", "S.Login")])
    ctx.xray.responses[KEYS] = rows_by([("PROJ-5", "Old name", None)])
    out, err = call(ctx, format="robot", content=ROBOT_DEF, allowCreateTests=True)
    assert not err and "status" not in out, out
    assert robot_tests(ctx) == [("Old name", ["PROJ-5"]), ("New", ["smoke"])]
    assert any("label" in w for w in out["warnings"]), out


@pytest.mark.parametrize("fmt,content,name", [
    ("cucumber", None, "Login"),
    ("robot", '<robot><suite name="S"><test name="T"/></suite></robot>', "S.T"),
    ("junit", '<testsuite><testcase classname="a" name="b"/></testsuite>', "a.b"),
])
def test_test_keys_must_be_existing_tests(tmp_path, fmt, content, name):
    ctx = match_ctx(tmp_path)
    ctx.xray.responses[KEYS] = rows_by([("PROJ-1", "x", None)])  # PROJ-2 is a story
    content = content or cucumber_report(untagged("Login"))
    out, err = call(ctx, format=fmt, content=content, testKeys={name: "PROJ-2"},
                    allowCreateTests=True)
    assert err and "PROJ-2" in out["error"] and "not" in out["error"], out
    assert ctx.xray.rest_calls == []
