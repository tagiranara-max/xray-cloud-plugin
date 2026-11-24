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
        "testExecIssue": {"id": "100", "key": "PROJ-9"}}
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
        "testExecIssue": {"id": "100", "key": "PROJ-9"}}
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
        "testExecIssue": {"id": "100", "key": "PROJ-9"}}
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


def test_cucumber_and_xray_json_endpoints(tmp_path):
    ctx = make(tmp_path)
    for fmt, path in (("cucumber", "import/execution/cucumber"),
                      ("behave", "import/execution/behave"), ("xray", "import/execution")):
        ctx.xray.rest_responses[("POST", path)] = {"testExecIssue": {"id": "7", "key": "PROJ-7"}}
        out, err = call(ctx, format=fmt, content="[]")
        assert not err, out
        m, p, kw = ctx.xray.rest_calls[-1]
        assert p == path and kw["data"] == b"[]" and kw["content_type"] == "application/json"
        assert "params" not in kw or not kw["params"]
        assert out["testExecKey"] == "PROJ-7"


def test_json_format_with_exec_or_env_params_refused(tmp_path):
    ctx = make(tmp_path)
    out, err = call(ctx, format="cucumber", content="[]", testEnvironments=["a"])
    assert err and ctx.xray.rest_calls == []


def test_json_format_new_execution_with_fields_multipart(tmp_path):
    ctx = make(tmp_path)
    ctx.xray.rest_responses[("POST", "import/execution/cucumber/multipart")] = {
        "testExecIssue": {"id": "7", "key": "PROJ-7"}}
    out, err = call(ctx, format="cucumber", content="[]", summary="S")
    assert not err, out
    files = ctx.xray.rest_calls[0][2]["multipart"]["files"]
    assert "result" in files and "results" not in files and "info" in files


def test_summary_counts_by_status(tmp_path):
    ctx = make(tmp_path, statuses=["PASSED", "FAILED", "PASSED", "TODO"])
    out, _ = call(ctx, format="junit", content="<x/>", detail=True)
    assert out["summary"] == {"total": 4, "byStatus": {"PASSED": 2, "FAILED": 1, "TODO": 1}}
    assert out["response"] == {"testExecIssue": {"id": "100", "key": "PROJ-9"}}
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
