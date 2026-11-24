from __future__ import annotations

import io
import json
import zipfile

import pytest

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = "projects:\n  PROJ: {}\n"
YAML_REQ = """
projects:
  PROJ:
    fields:
      test:
        Backend: {id: customfield_12345, type: option, required: true}
      precondition:
        Area: {id: customfield_10051, type: option, default: web}
"""
FEATURE = b"Feature: Checkout\n  Scenario: ok\n    Given x\n"


def call(ctx, tool, **args):
    args.setdefault("projectKey", "PROJ")
    res = REGISTRY.call(ctx, tool, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def imp_ctx(tmp_path, yaml=YAML, **kw):
    ctx = make_context(yaml, tmp_path=tmp_path, **kw)
    ctx.xray.rest_responses[("POST", "import/feature")] = {
        "errors": [], "updatedOrCreatedTests": [{"id": "1", "key": "PROJ-1"}],
        "updatedOrCreatedPreconditions": [{"id": "2", "key": "PROJ-2"}]}
    return ctx


def feature_file(tmp_path, name="a.feature", data=FEATURE):
    f = tmp_path / name
    f.write_bytes(data)
    return str(f)


def test_import_feature_multipart_parts_and_params(tmp_path):
    ctx = imp_ctx(tmp_path, YAML_REQ)
    f = feature_file(tmp_path)
    out, err = call(ctx, "import_feature_file", filePath=f, fields={"Backend": "Web"},
                    folder="/Checkout")
    assert not err
    m, path, kw = ctx.xray.rest_calls[0]
    assert (m, path) == ("POST", "import/feature")
    assert kw["params"] == {"projectKey": "PROJ", "folderPath": "/Checkout"}
    files = kw["multipart"]["files"]
    assert files["file"][1] == FEATURE
    assert json.loads(files["testInfo"][1])["fields"]["customfield_12345"]
    assert "customfield_10051" in json.loads(files["precondInfo"][1])["fields"]


def test_import_omits_empty_info_parts_and_folder(tmp_path):
    ctx = imp_ctx(tmp_path)
    out, err = call(ctx, "import_feature_file", filePath=feature_file(tmp_path))
    assert not err
    kw = ctx.xray.rest_calls[0][2]
    assert kw["params"] == {"projectKey": "PROJ"}
    assert set(kw["multipart"]["files"]) == {"file"}


def test_import_required_test_field_missing_refused_before_upload(tmp_path):
    ctx = imp_ctx(tmp_path, YAML_REQ)
    out, err = call(ctx, "import_feature_file", filePath=feature_file(tmp_path))
    assert err and "Backend" in json.dumps(out)
    assert ctx.xray.rest_calls == []


def test_import_rejects_other_extension(tmp_path):
    ctx = imp_ctx(tmp_path)
    out, err = call(ctx, "import_feature_file", filePath=feature_file(tmp_path, "a.txt"))
    assert err and ctx.xray.rest_calls == []


def test_import_blocked_in_read_only(tmp_path):
    ctx = imp_ctx(tmp_path, read_only=True)
    out, err = call(ctx, "import_feature_file", filePath=feature_file(tmp_path))
    assert err and ctx.xray.rest_calls == []


def test_import_result_lists_keys_and_errors(tmp_path):
    ctx = imp_ctx(tmp_path)
    ctx.xray.rest_responses[("POST", "import/feature")] = {
        "errors": [{"message": "bad"}],
        "updatedOrCreatedTests": [{"key": "PROJ-1"}, {"key": "PROJ-3"}],
        "updatedOrCreatedPreconditions": []}
    out, err = call(ctx, "import_feature_file", filePath=feature_file(tmp_path))
    assert not err
    assert out == {"tests": ["PROJ-1", "PROJ-3"], "preconditions": [],
                   "errors": [{"message": "bad"}]}


def export_ctx(tmp_path, body, **kw):
    ctx = make_context(YAML, tmp_path=tmp_path, **kw)
    ctx.xray.rest_responses[("GET", "export/cucumber")] = body
    return ctx


def test_export_writes_zip_entries(tmp_path):
    z = make_zip({"PROJ-1.feature": "Feature: A", "sub/PROJ-2.feature": "Feature: B",
                  "sub/": ""})
    ctx = export_ctx(tmp_path, z)
    d = tmp_path / "out"
    out, err = call(ctx, "export_cucumber_features", directory=str(d),
                    keys=["PROJ-1", "PROJ-2"])
    assert not err
    assert sorted(out["files"]) == ["PROJ-1.feature", "sub/PROJ-2.feature"]
    assert out["directory"] == str(d.resolve())
    assert (d / "sub" / "PROJ-2.feature").read_text() == "Feature: B"
    assert ctx.xray.rest_calls[0][2]["params"] == {"keys": "PROJ-1;PROJ-2"}


@pytest.mark.parametrize("bad", ["../evil.feature", "/abs.feature", "a/../../e.feature",
                                 "a\\b.feature", "C:/x.feature", "C:x.feature"])
def test_export_zip_slip_refused_nothing_written(tmp_path, bad):
    z = make_zip({"ok.feature": "Feature: A", bad: "Feature: evil"})
    ctx = export_ctx(tmp_path, z)
    d = tmp_path / "out"
    d.mkdir()
    out, err = call(ctx, "export_cucumber_features", directory=str(d), keys=["PROJ-1"])
    assert err and bad in out["entries"]
    assert list(d.iterdir()) == []
    assert not (tmp_path / "evil.feature").exists()


def test_export_existing_file_refused_unless_overwrite(tmp_path):
    z = make_zip({"a.feature": "NEW", "b.feature": "NEW"})
    ctx = export_ctx(tmp_path, z)
    d = tmp_path / "out"
    d.mkdir()
    (d / "a.feature").write_text("OLD")
    out, err = call(ctx, "export_cucumber_features", directory=str(d), keys=["PROJ-1"])
    assert err and "a.feature" in json.dumps(out)
    assert (d / "a.feature").read_text() == "OLD" and not (d / "b.feature").exists()
    out, err = call(ctx, "export_cucumber_features", directory=str(d), keys=["PROJ-1"],
                    overwrite=True)
    assert not err and (d / "a.feature").read_text() == "NEW"


def test_export_existing_directory_at_target_refused(tmp_path):
    z = make_zip({"a.feature": "NEW"})
    ctx = export_ctx(tmp_path, z)
    d = tmp_path / "out"
    (d / "a.feature").mkdir(parents=True)
    out, err = call(ctx, "export_cucumber_features", directory=str(d), keys=["PROJ-1"],
                    overwrite=True)
    assert err


def test_export_single_feature_body(tmp_path):
    ctx = export_ctx(tmp_path, b"Feature: Solo\n")
    d = tmp_path / "new" / "dir"
    out, err = call(ctx, "export_cucumber_features", directory=str(d), filter="12345")
    assert not err and out["files"] == ["export.feature"]
    assert (d / "export.feature").read_bytes() == b"Feature: Solo\n"
    assert ctx.xray.rest_calls[0][2]["params"] == {"filter": "12345"}


def test_export_keys_and_filter_exclusive(tmp_path):
    ctx = export_ctx(tmp_path, b"x")
    d = str(tmp_path / "o")
    for kw in ({}, {"keys": ["PROJ-1"], "filter": "1"}, {"keys": []}):
        out, err = call(ctx, "export_cucumber_features", directory=d, **kw)
        assert err
    out, err = call(ctx, "export_cucumber_features", directory=d, keys=["OTHER-1"])
    assert err
    assert ctx.xray.rest_calls == []


def test_export_not_blocked_by_read_only(tmp_path):
    ctx = export_ctx(tmp_path, b"Feature: x", read_only=True)
    out, err = call(ctx, "export_cucumber_features", directory=str(tmp_path / "o"),
                    keys=["PROJ-1"])
    assert not err
