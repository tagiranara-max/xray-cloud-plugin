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
