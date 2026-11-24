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


@pytest.mark.parametrize("fmt", ["cucumber", "behave"])
def test_cucumber_behave_new_execution_always_multipart_with_project(tmp_path, fmt):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution/%s/multipart" % fmt)] = {
        "id": "7", "key": "PROJ-7"}
    out, err = call(ctx, format=fmt, content="[]")
    assert not err, out
    m, p, kw = ctx.xray.rest_calls[0]
    assert p == "import/execution/%s/multipart" % fmt
    files = kw["multipart"]["files"]
    info = json.loads(files["info"][1])
    assert info["fields"]["project"] == {"key": "PROJ"}
    assert info["fields"]["issuetype"] == {"name": "Test Execution"}
    assert "xrayFields" not in info
    assert files["results"][1] == b"[]"
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
    out, err = call(ctx, format="cucumber", content="[]", testEnvironments=["a"],
                    testPlanKey="PROJ-5")
    assert not err, out
    m, path, kw = ctx.xray.rest_calls[0]
    assert path == "import/execution/cucumber/multipart"
    info = json.loads(kw["multipart"]["files"]["info"][1])
    assert info["xrayFields"] == {"testPlanKey": "PROJ-5", "environments": ["a"]}
    assert kw["multipart"]["files"]["results"][1] == b"[]"


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


@pytest.mark.parametrize("tags", [[], ["@TEST_PROJ-1", "@TEST_PROJ-2"], ["@PROJ-1"]])
def test_cucumber_into_existing_execution_needs_one_test_tag(tmp_path, tags):
    ctx = make(tmp_path)
    report = cucumber_report(_scenario(["@TEST_PROJ-1"], _step("passed")),
                             _scenario(tags, _step("passed")))
    out, err = call(ctx, format="cucumber", content=report, testExecKey="PROJ-9")
    assert err and "@TEST_" in out["error"] and out["scenarios"] == ["F / S %s" % tags]
    assert ctx.xray.rest_calls == []


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


def test_behave_into_existing_execution_refused(tmp_path):
    ctx = make(tmp_path)
    out, err = call(ctx, format="behave", content="[]", testExecKey="PROJ-9")
    assert err and "new Test Execution" in out["error"] and ctx.xray.rest_calls == []


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
    out, err = call(ctx, format="cucumber", content="[]", summary="S")
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
