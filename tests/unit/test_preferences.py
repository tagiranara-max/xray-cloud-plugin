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
