"""Gemini CLI extension manifest checks."""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "gemini-extension.json"


def load():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_gemini_manifest_points_at_server():
    server = load()["mcpServers"]["xray"]
    assert server["command"] == "python3"
    path = server["args"][0].replace("${extensionPath}", str(REPO))
    assert Path(path).is_file()


def test_gemini_manifest_has_no_secrets():
    data = load()
    for setting in data["settings"]:
        assert set(setting) <= {"name", "description", "envVar", "sensitive"}
        assert setting["envVar"].isupper()
    assert {"XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "JIRA_API_TOKEN"} <= {
        s["envVar"] for s in data["settings"] if s.get("sensitive")
    }
    for value in data["mcpServers"]["xray"].get("env", {}).values():
        assert value.startswith("${") and value.endswith("}")
