from __future__ import annotations

import json

from conftest import make_context
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401
from xray_cloud.tools import tagtools

YAML = """
projects:
  P: {}
featureImport:
  tagRules:
    - tag: suite
      label: "{value}"
"""
GOOD = "@suite=smoke @wip\nFeature: F\n  @area=api\n  Scenario: A\n    Given a\n"


def call(ctx, **args):
    res = REGISTRY.call(ctx, "analyze_feature_tags", args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def ctx_for(root, sets=(), yaml=YAML):
    def find(v):
        return {"getTestSets": {"results": [
            {"issueId": str(i), "jira": {"key": "P-%d" % i, "summary": s}}
            for i, s in enumerate(sets, 1)]}}
    ctx = make_context(yaml, responses={"FindTestSets": find})
    ctx.env["CLAUDE_PROJECT_DIR"] = str(root)
    return ctx


def test_registered_as_read_tool():
    t = REGISTRY.get("analyze_feature_tags")
    assert t.write is False
    d = t.description
    for needle in ("set_preferences", "example", "unmatched", "nothing until the user agrees"):
        assert needle in d


def test_summary_for_files_and_set_lookup(tmp_path):
    (tmp_path / "a.feature").write_text(GOOD, encoding="utf-8")
    ctx = ctx_for(tmp_path, sets=["api"])
    res, err = call(ctx, projectKey="P", featureFilePaths=["a.feature"])
    assert not err, res
    s = res["tagSummary"]
    assert s["handledByRules"] == ["suite=smoke"]
    assert s["unmatched"] == ["wip", "area=api"]
    assert s["valuesMatchingTestSets"] == {"area": ["api"]}
    assert res["files"] == ["a.feature"] and res["errors"] == []
    assert res["rules"] == {"count": 1, "source": "settings"}
    assert [op for op, _ in ctx.xray.calls] == ["FindTestSets"]


def test_folder_recursion_parse_errors_reported_per_file(tmp_path):
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "a.feature").write_text(GOOD, encoding="utf-8")
    (tmp_path / "d" / "sub" / "b.feature").write_text(GOOD, encoding="utf-8")
    (tmp_path / "d" / "bad.feature").write_text("Scenario: A\n Given x\n", encoding="utf-8")
    (tmp_path / "d" / "note.txt").write_text("x", encoding="utf-8")
    res, err = call(ctx_for(tmp_path), projectKey="P", folder="d")
    assert not err, res
    assert res["files"] == ["d/a.feature", "d/sub/b.feature"]
    assert [e["path"] for e in res["errors"]] == ["d/bad.feature"] and res["errors"][0]["error"]
    assert res["tagSummary"]["keyValue"]["suite"]["values"] == {"smoke": 2}
    assert "capped" not in res or res["capped"] is False


def test_folder_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(tagtools, "MAX_FILES", 2)
    for i in range(4):
        (tmp_path / ("f%d.feature" % i)).write_text(GOOD, encoding="utf-8")
    res, err = call(ctx_for(tmp_path), projectKey="P", folder=".")
    assert not err, res
    assert len(res["files"]) == 2 and res["capped"] is True and res["maxFiles"] == 2


def test_requires_input_and_known_project(tmp_path):
    res, err = call(ctx_for(tmp_path), projectKey="P")
    assert err and "featureFilePaths" in res["error"]
    res, err = call(ctx_for(tmp_path), projectKey="NOPE", folder=".")
    assert err
