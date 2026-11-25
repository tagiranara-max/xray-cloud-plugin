from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import xray_cloud.tools  # noqa: F401
from xray_cloud.registry import REGISTRY

MAIN = Path(__file__).resolve().parents[2] / "plugin" / "server" / "main.py"


def _env(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"projects": {"PROJ": {}}}))
    data = tmp_path / "data"
    data.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "XRAY_CLIENT_ID": "xid-secret-1",
           "XRAY_CLIENT_SECRET": "xsec-secret-2", "XRAY_SETTINGS_FILE": str(settings),
           "XRAY_DATA_DIR": str(data)}
    env.update(extra)
    return env, settings, data


def _run(tmp_path, msgs, **extra):
    env, settings, data = _env(tmp_path, **extra)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = subprocess.run([sys.executable, str(MAIN)], input="".join(json.dumps(m) + "\n" for m in msgs),
                          capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd))
    return proc, settings, data


def _msgs():
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_status", "arguments": {"check": False}}},
    ]


def _status(proc):
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    parsed = [json.loads(l) for l in lines]
    assert [p["id"] for p in parsed] == [1, 2, 3]
    return parsed, json.loads(parsed[2]["result"]["content"][0]["text"])


def test_stdio_e2e_without_claude_env(tmp_path):
    proc, settings, data = _run(tmp_path, _msgs())
    assert proc.returncode == 0, proc.stderr
    parsed, status = _status(proc)
    assert len(parsed[1]["result"]["tools"]) == len(REGISTRY.list())
    assert status["settings"]["path"] == str(settings)
    assert status["preferencesFile"].startswith(str(data))
    assert len(status["toolGroups"]) > 1
    for secret in ("xid-secret-1", "xsec-secret-2"):
        assert secret not in proc.stdout and secret not in proc.stderr


def test_tool_groups_all_when_unset_and_filtered_by_env(tmp_path):
    from xray_cloud.toolgroups import GROUPS
    (tmp_path / "a").mkdir()
    proc, _, _ = _run(tmp_path / "a", _msgs())
    assert _status(proc)[1]["toolGroups"] == list(GROUPS)
    (tmp_path / "b").mkdir()
    proc, _, _ = _run(tmp_path / "b", _msgs(), XRAY_TOOLS="analysis")
    parsed, status = _status(proc)
    assert status["toolGroups"] == ["analysis", "setup"]
    assert len(parsed[1]["result"]["tools"]) < len(REGISTRY.list())


def test_bogus_xray_tools_exits_2_with_valid_groups(tmp_path):
    from xray_cloud.toolgroups import GROUPS
    proc, _, _ = _run(tmp_path, _msgs(), XRAY_TOOLS="bogus")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "Unknown XRAY_TOOLS" in proc.stderr
    for g in GROUPS:
        assert g in proc.stderr


def test_empty_plugin_prompts_do_not_hide_environment_credentials(tmp_path):
    """Claude Code with blank prompts: *_SETTING arrive empty / as placeholders, the shell
    variables must still configure the server."""
    proc, _, _ = _run(tmp_path, _msgs(), XRAY_CLIENT_ID_SETTING="",
                      XRAY_CLIENT_SECRET_SETTING="${user_config.xray_client_secret}",
                      XRAY_REGION_SETTING="")
    assert proc.returncode == 0, proc.stderr
    _, status = _status(proc)
    assert status["credentials"]["configured"] is True
