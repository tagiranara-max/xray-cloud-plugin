from __future__ import annotations

import pytest

from conftest import make_context
from xray_cloud import choices
from xray_cloud.errors import ToolError
from xray_cloud.preferences import PreferenceStore

YAML = """\
featureImport:
  createTestSet: true
  backgrounds: inline
aiCreated: {label: top-label, scope: all}
projects:
  P1:
    featureImport: {createTestSet: false}
    aiCreated: {label: null}
  P2: {}
"""


def ctx_for(tmp_path, yaml=YAML, region="global"):
    ctx = make_context(yaml, tmp_path=tmp_path)
    ctx.env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "data")
    ctx.env["XRAY_REGION"] = region
    ctx.preferences = PreferenceStore(ctx.env)
    return ctx


def test_context_exposes_preferences(tmp_path):
    ctx = make_context("", tmp_path=tmp_path)
    assert isinstance(ctx.preferences, PreferenceStore)


def test_choices_catalog():
    assert set(choices.CHOICES) == {"createTestSet", "backgrounds", "tagsAsLabels",
                                    "unmatchedTags", "aiLabel", "aiLabelScope", "tagRules"}
    assert choices.CHOICES["createTestSet"]["kind"] == "bool"
    assert choices.CHOICES["backgrounds"]["kind"] == "enum"
    assert choices.CHOICES["aiLabel"]["kind"] == "label"
    assert choices.CHOICES["tagRules"]["kind"] == "rules"
    assert all(c["question"] for c in choices.CHOICES.values())


def test_call_value_wins_over_everything(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.preferences.update("global", "P2", {"createTestSet": False})
    assert choices.resolve(ctx, "P2", "createTestSet", False) == (False, "call")


def test_project_settings_beat_top_level(tmp_path):
    ctx = ctx_for(tmp_path)
    assert choices.resolve(ctx, "P1", "createTestSet", None) == (False, "settings")
    assert choices.resolve(ctx, "P2", "createTestSet", None) == (True, "settings")
    assert choices.resolve(ctx, "P1", "backgrounds", None) == ("inline", "settings")


def test_settings_beat_remembered(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.preferences.update("global", "P2", {"createTestSet": False})
    assert choices.resolve(ctx, "P2", "createTestSet", None) == (True, "settings")


def test_remembered_used_when_no_settings(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.preferences.update("global", "P2", {"tagsAsLabels": True})
    assert choices.resolve(ctx, "P2", "tagsAsLabels", None) == (True, "remembered")


def test_unresolved_is_none_none(tmp_path):
    ctx = ctx_for(tmp_path)
    for name in ("tagsAsLabels", "unmatchedTags", "tagRules"):
        assert choices.resolve(ctx, "P2", name, None) == (None, None)


def test_works_without_settings_file(tmp_path):
    ctx = ctx_for(tmp_path, yaml="")
    assert choices.resolve(ctx, "P1", "createTestSet", None) == (None, None)


def test_ai_label_mapping_and_null_counts_as_answer(tmp_path):
    ctx = ctx_for(tmp_path)
    assert choices.resolve(ctx, "P1", "aiLabel", None) == ("", "settings")
    assert choices.resolve(ctx, "P2", "aiLabel", None) == ("top-label", "settings")
    assert choices.resolve(ctx, "P1", "aiLabelScope", None) == ("all", "settings")


def test_tag_rules_replace_not_merge(tmp_path):
    y = ("featureImport: {tagRules: [{tag: a, label: x}]}\nprojects:\n  P1:\n"
         "    featureImport: {tagRules: [{tag: b, label: x}]}\n  P2: {}\n")
    ctx = ctx_for(tmp_path, yaml=y)
    r1, src1 = choices.resolve(ctx, "P1", "tagRules", None)
    r2, src2 = choices.resolve(ctx, "P2", "tagRules", None)
    assert (src1, src2) == ("settings", "settings")
    assert [r.keys for r in r1] == [["b"]] and [r.keys for r in r2] == [["a"]]


def test_remembered_isolated_per_region_and_project(tmp_path):
    ctx = ctx_for(tmp_path, yaml="")
    choices.remember(ctx, "P1", {"tagsAsLabels": True}, False)
    assert choices.resolve(ctx, "P1", "tagsAsLabels", None) == (True, "remembered")
    assert choices.resolve(ctx, "P2", "tagsAsLabels", None) == (None, None)
    ctx.env["XRAY_REGION"] = "eu"
    assert choices.resolve(ctx, "P1", "tagsAsLabels", None) == (None, None)


def test_remember_stores_when_absent_and_ignores_identical(tmp_path):
    ctx = ctx_for(tmp_path, yaml="")
    choices.remember(ctx, "P1", {"backgrounds": "inline"}, False)
    assert ctx.preferences.get("global", "P1") == {"backgrounds": "inline"}
    choices.remember(ctx, "P1", {"backgrounds": "inline"}, True)
    assert ctx.preferences.get("global", "P1") == {"backgrounds": "inline"}


def test_remember_differing_needs_force(tmp_path):
    ctx = ctx_for(tmp_path, yaml="")
    choices.remember(ctx, "P1", {"backgrounds": "inline", "tagsAsLabels": True}, False)
    choices.remember(ctx, "P1", {"backgrounds": "preconditions", "createTestSet": True}, False)
    assert ctx.preferences.get("global", "P1") == {
        "backgrounds": "inline", "tagsAsLabels": True, "createTestSet": True}
    choices.remember(ctx, "P1", {"backgrounds": "preconditions"}, True)
    assert ctx.preferences.get("global", "P1")["backgrounds"] == "preconditions"


def test_remember_skips_none_and_unknown_names(tmp_path):
    ctx = ctx_for(tmp_path, yaml="")
    choices.remember(ctx, "P1", {"backgrounds": None, "bogus": 1}, True)
    assert ctx.preferences.get("global", "P1") == {}


def test_remember_ai_label_empty_string_is_an_answer(tmp_path):
    ctx = ctx_for(tmp_path, yaml="")
    choices.remember(ctx, "P1", {"aiLabel": ""}, False)
    assert choices.resolve(ctx, "P1", "aiLabel", None) == ("", "remembered")


def test_question_shape():
    q = choices.question("backgrounds", {"count": 2})
    assert q["id"] == "backgrounds" and q["question"] and q["context"] == {"count": 2}
    assert q["options"] == ["preconditions", "inline"]
    assert "options" not in choices.question("aiLabel", {})
    assert choices.question("createTestSet", {})["options"] == [True, False]
    with pytest.raises(KeyError):
        choices.question("nope", {})


@pytest.mark.parametrize("name,value", [
    ("createTestSet", "yes"), ("tagsAsLabels", 1), ("backgrounds", "both"),
    ("unmatchedTags", "drop"), ("aiLabel", "a b"), ("aiLabel", 5), ("aiLabelScope", "some"),
    ("tagRules", {"tag": "x"})])
def test_invalid_remembered_value_is_refused_naming_file_and_key(tmp_path, name, value):
    """Final fix 10: hand-edited preferences are validated on read."""
    ctx = ctx_for(tmp_path, yaml="projects:\n  P2: {}\n")
    ctx.preferences.update("global", "P2", {name: value})
    with pytest.raises(ToolError) as exc:
        choices.resolve(ctx, "P2", name, None)
    msg = str(exc.value)
    assert str(ctx.preferences.path) in msg and name in msg and "global/P2" in msg


@pytest.mark.parametrize("name,value", [
    ("createTestSet", False), ("backgrounds", "inline"), ("aiLabel", ""), ("aiLabel", "ai-made"),
    ("aiLabelScope", "all"), ("unmatchedTags", "skip"), ("tagRules", [])])
def test_valid_remembered_values_pass(tmp_path, name, value):
    ctx = ctx_for(tmp_path, yaml="projects:\n  P2: {}\n")
    ctx.preferences.update("global", "P2", {name: value})
    assert choices.resolve(ctx, "P2", name, None) == (value, "remembered")


def test_region_of_ignores_placeholder_and_whitespace():
    from types import SimpleNamespace
    from xray_cloud.choices import region_of
    assert region_of(SimpleNamespace(env={"XRAY_REGION": "${env:XRAY_REGION}"})) == "global"
    assert region_of(SimpleNamespace(env={"XRAY_REGION": "  "})) == "global"
    assert region_of(SimpleNamespace(env={"XRAY_REGION": " EU "})) == "eu"
