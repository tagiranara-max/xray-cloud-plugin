"""Live check of the stdio server started like a non-Claude client would (read-only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[2] / "plugin" / "server" / "main.py"
_KEYS = ("XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "XRAY_REGION", "XRAY_TEST_PROJECT",
         "JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")


def _has(*names):
    return all((os.environ.get(n) or "").strip() for n in names)


@pytest.mark.skipif(not _has("XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "XRAY_TEST_PROJECT"),
                    reason="live Xray credentials / XRAY_TEST_PROJECT not set")
def test_live_stdio_plain_env(tmp_path):
    project = os.environ["XRAY_TEST_PROJECT"].strip()
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"projects": {project: {}}}))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "XRAY_SETTINGS_FILE": str(settings),
           "XRAY_DATA_DIR": str(data)}
    env.update({k: os.environ[k] for k in _KEYS if (os.environ.get(k) or "").strip()})
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_status", "arguments": {"check": True}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_tests", "arguments": {"projectKey": project, "jql": "project = %s" % project, "limit": 3}}},
    ]
    proc = subprocess.run([sys.executable, str(MAIN)], input="".join(json.dumps(m) + "\n" for m in msgs),
                          capture_output=True, text=True, timeout=120, env=env, cwd=str(cwd))
    assert proc.returncode == 0, proc.stderr[-500:]
    parsed = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert [p["id"] for p in parsed] == [1, 2, 3]
    for p in parsed[1:]:
        assert not p["result"].get("isError"), json.dumps(p)[:500]
    status = json.loads(parsed[1]["result"]["content"][0]["text"])
    assert status["settings"]["path"] == str(settings)
    assert status["preferencesFile"].startswith(str(data))
    json.loads(parsed[2]["result"]["content"][0]["text"])
    for k in ("XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "JIRA_API_TOKEN"):
        v = (os.environ.get(k) or "").strip()
        if v:
            assert v not in proc.stdout and v not in proc.stderr
