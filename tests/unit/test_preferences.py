from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xray_cloud.errors import ToolError
from xray_cloud.preferences import PreferenceStore


def test_path_uses_plugin_data(tmp_path):
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")})
    assert s.path == tmp_path / "data" / "preferences.json"


def test_path_falls_back_to_home_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = PreferenceStore({})
    assert s.path == Path(os.path.expanduser("~")) / ".config" / "xray-cloud" / "preferences.json"
    assert PreferenceStore({"CLAUDE_PLUGIN_DATA": ""}).path == s.path


def test_get_missing_file_is_empty(tmp_path):
    assert PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path)}).get("global", "P") == {}


def test_update_get_clear_roundtrip(tmp_path):
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path / "d")})
    assert s.update("global", "P", {"a": 1, "b": "x"}) == {"a": 1, "b": "x"}
    assert s.update("global", "P", {"b": "y"}) == {"a": 1, "b": "y"}
    assert s.get("global", "P") == {"a": 1, "b": "y"}
    assert s.clear("global", "P", ["a", "missing"]) == {"b": "y"}
    assert s.get("global", "P") == {"b": "y"}
    raw = json.loads(s.path.read_text())
    assert raw == {"global/P": {"b": "y"}}


def test_keys_separated_per_region_and_project(tmp_path):
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    s.update("global", "P", {"a": 1})
    s.update("eu", "P", {"a": 2})
    s.update("global", "Q", {"a": 3})
    assert [s.get(r, p)["a"] for r, p in (("global", "P"), ("eu", "P"), ("global", "Q"))] == [1, 2, 3]
    assert s.get("au", "P") == {}


def test_clear_last_name_drops_entry(tmp_path):
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    s.update("global", "P", {"a": 1})
    s.clear("global", "P", ["a"])
    assert json.loads(s.path.read_text()) == {}


def test_corrupt_json_raises_naming_file(tmp_path):
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    s.path.write_text("{not json")
    for call in (lambda: s.get("global", "P"), lambda: s.update("global", "P", {"a": 1})):
        with pytest.raises(ToolError) as ei:
            call()
        assert str(s.path) in ei.value.message
    s.path.write_text("[1]")
    with pytest.raises(ToolError):
        s.get("global", "P")


def test_write_goes_through_temp_file_and_rename(tmp_path, monkeypatch):
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (seen.append((a, b)), real(a, b))[1])
    s.update("global", "P", {"a": 1})
    assert len(seen) == 1 and str(seen[0][1]) == str(s.path) and str(seen[0][0]) != str(s.path)
    assert os.listdir(str(tmp_path)) == ["preferences.json"]


def test_failed_write_leaves_old_file_intact(tmp_path, monkeypatch):
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    s.update("global", "P", {"a": 1})

    def boom(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(ToolError) as ei:
        s.update("global", "P", {"a": 2})
    assert str(s.path) in ei.value.message
    monkeypatch.undo()
    assert s.get("global", "P") == {"a": 1}
    assert os.listdir(str(tmp_path)) == ["preferences.json"]


def test_unwritable_directory_raises_tool_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(blocker / "sub")})
    with pytest.raises(ToolError) as ei:
        s.update("global", "P", {"a": 1})
    assert "preferences.json" in ei.value.message


def test_preferences_dir_prefers_xray_data_dir(tmp_path):
    both = {"XRAY_DATA_DIR": str(tmp_path / "x"), "CLAUDE_PLUGIN_DATA": str(tmp_path / "c")}
    assert PreferenceStore(both).path == tmp_path / "x" / "preferences.json"
    only = {"CLAUDE_PLUGIN_DATA": str(tmp_path / "c")}
    assert PreferenceStore(only).path == tmp_path / "c" / "preferences.json"
    assert PreferenceStore({}).path == (Path(os.path.expanduser("~")) / ".config"
                                        / "xray-cloud" / "preferences.json")


@pytest.mark.parametrize("bad", ["${env:XRAY_DATA_DIR}", "  "])
def test_placeholder_data_dir_falls_back(bad, monkeypatch, tmp_path):
    s = PreferenceStore({"XRAY_DATA_DIR": bad, "CLAUDE_PLUGIN_DATA": str(tmp_path / "p")})
    assert s.path == tmp_path / "p" / "preferences.json"
    monkeypatch.setenv("HOME", str(tmp_path))
    s = PreferenceStore({"XRAY_DATA_DIR": bad, "CLAUDE_PLUGIN_DATA": bad})
    assert s.path == tmp_path / ".config" / "xray-cloud" / "preferences.json"


# site-scoped approvals and field values

from xray_cloud.preferences import site_id  # noqa: E402

CREDS = {"XRAY_CLIENT_ID": "id-SECRET-111", "XRAY_CLIENT_SECRET": "sec-SECRET-222"}


def _store(tmp_path):
    return PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path / "d")})


def test_site_id_is_hash_prefix_and_never_raw_id():
    import hashlib
    sid = site_id(CREDS)
    assert sid == hashlib.sha256(b"id-SECRET-111").hexdigest()[:12] and len(sid) == 12
    assert "SECRET" not in sid
    assert site_id({}) == ""
    assert site_id({"XRAY_CLIENT_ID": "x"}) == ""
    assert site_id(dict(CREDS, XRAY_REGION="bogus")) == ""


def test_approve_and_revoke_project_and_all(tmp_path):
    s = _store(tmp_path)
    assert s.approval("global", "abc") == {"approveAll": False, "approvedProjects": []}
    s.approve("global", "abc", "PROJ", False)
    s.approve("global", "abc", "BACK", False)
    assert s.approval("global", "abc") == {"approveAll": False,
                                           "approvedProjects": ["BACK", "PROJ"]}
    s.approve("global", "abc", None, True)
    assert s.approval("global", "abc")["approveAll"] is True
    s.revoke("global", "abc", "PROJ", False)
    s.revoke("global", "abc", None, True)
    assert s.approval("global", "abc") == {"approveAll": False, "approvedProjects": ["BACK"]}
    assert json.loads(s.path.read_text())["global/abc"] == {
        "approveAll": False, "approvedProjects": ["BACK"]}


def test_old_choice_key_read_then_migrated(tmp_path):
    s = _store(tmp_path)
    s.path.parent.mkdir(parents=True)
    s.path.write_text(json.dumps({"global/PROJ": {"createTestSet": True}}))
    assert s.get("global", "PROJ", "abc") == {"createTestSet": True}
    assert json.loads(s.path.read_text()) == {"global/PROJ": {"createTestSet": True}}
    s.update("global", "PROJ", {"backgrounds": "inline"}, "abc")
    assert json.loads(s.path.read_text()) == {
        "global/abc/PROJ": {"createTestSet": True, "backgrounds": "inline"}}
    assert s.get("global", "PROJ", "abc")["createTestSet"] is True


def test_other_site_has_no_approval(tmp_path):
    s = _store(tmp_path)
    s.approve("global", "siteA", "PROJ", True)
    assert s.approval("global", "siteB") == {"approveAll": False, "approvedProjects": []}
    assert s.approval("eu", "siteA") == {"approveAll": False, "approvedProjects": []}
    assert s.approval("global", "") == {"approveAll": False, "approvedProjects": []}


def test_concurrent_write_merges_not_drops(tmp_path):
    a, b = _store(tmp_path), _store(tmp_path)
    real_load, state = b._load, {"fired": False}

    def racing_load():  # A writes right after B loaded, before B saves
        data = real_load()
        if not state["fired"]:
            state["fired"] = True
            a.approve("global", "abc", "PROJ", False)
        return data
    b._load = racing_load
    b.approve("global", "abc", "BACK", False)
    assert state["fired"]
    assert a.approval("global", "abc")["approvedProjects"] == ["BACK", "PROJ"]


def test_old_and_new_keys_both_present_are_merged(tmp_path):
    s = _store(tmp_path)
    s.path.parent.mkdir(parents=True)
    s.path.write_text(json.dumps({"global/PROJ": {"createTestSet": True, "backgrounds": "inline"},
                                  "global/abc/PROJ": {"backgrounds": "preconditions",
                                                      "tagsAsLabels": False}}))
    assert s.get("global", "PROJ", "abc") == {
        "createTestSet": True, "backgrounds": "preconditions", "tagsAsLabels": False}
    s.update("global", "PROJ", {"aiLabel": "x"}, "abc")
    assert json.loads(s.path.read_text()) == {"global/abc/PROJ": {
        "createTestSet": True, "backgrounds": "preconditions", "tagsAsLabels": False,
        "aiLabel": "x"}}


def test_revoke_without_entry_writes_nothing(tmp_path):
    s = _store(tmp_path)
    s.revoke("global", "abc", "PROJ", True)
    assert not s.path.exists()


def test_field_values_remember_clear(tmp_path):
    s = _store(tmp_path)
    assert s.field_values("global", "abc", "PROJ", "test") == {}
    s.remember_fields("global", "abc", "PROJ", "test", {"customfield_12345": "Backend"})
    s.remember_fields("global", "abc", "PROJ", "test", {"customfield_2": 1})
    s.remember_fields("global", "abc", "PROJ", "plan", {"customfield_3": "z"})
    assert s.field_values("global", "abc", "PROJ", "test") == {
        "customfield_12345": "Backend", "customfield_2": 1}
    assert s.field_values("global", "other", "PROJ", "test") == {}
    s.clear_fields("global", "abc", "PROJ", "test")
    assert s.field_values("global", "abc", "PROJ", "test") == {}
    assert s.field_values("global", "abc", "PROJ", "plan") == {"customfield_3": "z"}
    s.clear_fields("global", "abc", "PROJ", None)
    assert json.loads(s.path.read_text()) == {}
