"""Gemini CLI extension manifest checks."""
import json
import re
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


def test_gemini_manifest_version_placeholder_and_workspace_dir():
    data = load()
    assert data["version"] == "0.0.0"
    assert data["mcpServers"]["xray"]["env"] == {"XRAY_PROJECT_DIR": "${workspacePath}"}


def test_gemini_env_vars_valid_and_unique():
    names = [s["envVar"] for s in load()["settings"]]
    assert all(re.match(r"^[A-Z][A-Z0-9_]*$", n) for n in names)
    assert len(names) == len(set(names))
    assert "XRAY_TOOLS" in names
    assert "XRAY_DATA_DIR" not in names and "XRAY_PROJECT_DIR" not in names


def test_gemini_settings_match_plugin_json_user_config():
    plugin = json.loads((REPO / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    gemini = {s["envVar"]: bool(s.get("sensitive")) for s in load()["settings"]}
    for env, ref in plugin["mcpServers"]["xray"]["env"].items():
        key = ref[len("${user_config."):-1]
        assert env in gemini, env
        assert gemini[env] == bool(plugin["userConfig"][key].get("sensitive")), env
