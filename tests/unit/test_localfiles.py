from __future__ import annotations

import os

import pytest

from xray_cloud import localfiles
from xray_cloud.errors import ToolError
from xray_cloud.localfiles import read_local_file, resolve_local_path


def test_relative_path_resolves_against_project_dir(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hi")
    full, data = read_local_file({"CLAUDE_PROJECT_DIR": str(tmp_path)}, "a.txt")
    assert data == b"hi" and full == os.path.realpath(str(tmp_path / "a.txt"))


def test_relative_path_falls_back_to_cwd(tmp_path, monkeypatch):
    (tmp_path / "b.txt").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    assert read_local_file({}, "b.txt")[1] == b"x"


def test_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "c.txt").write_bytes(b"c")
    assert resolve_local_path({}, "~/c.txt") == os.path.realpath(str(tmp_path / "c.txt"))


def test_missing_directory_and_empty_paths_are_clear_errors(tmp_path):
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    with pytest.raises(ToolError, match="Evidence file not found: nope.png"):
        read_local_file(env, "nope.png", "Evidence file")
    with pytest.raises(ToolError, match="is a directory"):
        read_local_file(env, str(tmp_path))
    with pytest.raises(ToolError, match="non-empty"):
        read_local_file(env, " ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_file_is_clear_error(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_bytes(b"x")
    f.chmod(0)
    try:
        with pytest.raises(ToolError, match="not readable"):
            read_local_file({}, str(f))
    finally:
        f.chmod(0o600)


def test_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(localfiles, "MAX_FILE_BYTES", 4)
    (tmp_path / "big.bin").write_bytes(b"12345")
    (tmp_path / "ok.bin").write_bytes(b"1234")
    with pytest.raises(ToolError, match="too large") as ei:
        read_local_file({}, str(tmp_path / "big.bin"), "Attachment file")
    assert ei.value.data["sizeBytes"] == 5
    assert read_local_file({}, str(tmp_path / "ok.bin"))[1] == b"1234"


@pytest.fixture
def in_plugin_dir(tmp_path, monkeypatch):
    """cwd inside the plugin's own folder, no project variables: the project dir is unknown."""
    from xray_cloud import config
    plugin = tmp_path / "plugin"
    (plugin / "server").mkdir(parents=True)
    monkeypatch.setattr(config, "PLUGIN_DIR", plugin)
    monkeypatch.chdir(plugin / "server")
    return plugin


def test_relative_path_refused_when_project_dir_unknown(in_plugin_dir):
    (in_plugin_dir / "server" / "f.feature").write_bytes(b"x")
    with pytest.raises(ToolError) as e:
        read_local_file({}, "f.feature")
    assert "absolute path" in e.value.message and "XRAY_PROJECT_DIR" in e.value.message
    assert e.value.data["reason"] == "project_dir_unknown"


def test_absolute_and_tilde_paths_work_when_project_dir_unknown(in_plugin_dir, tmp_path, monkeypatch):
    target = tmp_path / "abs.txt"
    target.write_bytes(b"a")
    assert read_local_file({}, str(target))[1] == b"a"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert read_local_file({}, "~/abs.txt")[1] == b"a"


def test_project_variable_wins_over_plugin_cwd(in_plugin_dir, tmp_path):
    (tmp_path / "p.txt").write_bytes(b"p")
    assert read_local_file({"XRAY_PROJECT_DIR": str(tmp_path)}, "p.txt")[1] == b"p"
