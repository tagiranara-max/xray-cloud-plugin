from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "plugin" / "server" / "main.py"


def test_server_starts_without_credentials_and_stdout_is_json_only():
    env = {k: v for k, v in os.environ.items() if not k.startswith("XRAY")}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.run([sys.executable, str(MAIN)], input="".join(json.dumps(m) + "\n" for m in msgs),
                          capture_output=True, text=True, timeout=30, env=env)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    parsed = [json.loads(l) for l in lines]  # every stdout line must be JSON
    assert [p["id"] for p in parsed] == [1, 2]
    assert parsed[0]["result"]["serverInfo"]["name"] == "xray-cloud"
    assert "tools" in parsed[1]["result"]


def test_non_ascii_round_trip_under_c_locale():
    env = {k: v for k, v in os.environ.items() if not k.startswith("XRAY")}
    env.update({"LANG": "C", "LC_ALL": "C"})
    env.pop("PYTHONIOENCODING", None)
    # Disable Python's own C-locale UTF-8 coercion so only main.py's guard can save us.
    env.update({"PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"})
    # json.dumps escapes to \u00eb, so the server itself must emit the real character.
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "tëst", "arguments": {}}}
    proc = subprocess.run([sys.executable, str(MAIN)], input=(json.dumps(msg) + "\n").encode("ascii"),
                          capture_output=True, timeout=30, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out = json.loads(proc.stdout.decode("utf-8"))
    assert out["result"]["isError"] is True
    assert "tëst" in json.loads(out["result"]["content"][0]["text"])["error"]
