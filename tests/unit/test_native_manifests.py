"""Codex and Cursor native plugin manifests: they parse, point at files that exist, start
plugin/server/main.py, ship the four skills and carry no secret values."""
import json
import re
import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugin"
SERVER_MAIN = PLUGIN / "server" / "main.py"
SKILLS = {"xray-setup", "import-results", "import-tests", "predict-affected-tests"}

CODEX_MARKETPLACE = REPO / ".agents" / "plugins" / "marketplace.json"
CODEX_MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
CURSOR_MARKETPLACE = REPO / ".cursor-plugin" / "marketplace.json"
CURSOR_MANIFEST = PLUGIN / ".cursor-plugin" / "plugin.json"
CURSOR_MCP = PLUGIN / "mcp.json"
CLAUDE_MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def claude_env_names():
    """The env names Claude Code's plugin.json passes to the server (from userConfig)."""
    return {n[:-len("_SETTING")] for n in load(CLAUDE_MANIFEST)["mcpServers"]["xray"]["env"]}


def assert_relative(path):
    assert isinstance(path, str) and path, path
    assert not path.startswith("/") and not re.match(r"^[A-Za-z]:", path), path
    assert ".." not in Path(path).parts, path


# --- Codex -----------------------------------------------------------------------------

def test_codex_marketplace_entry():
    data = load(CODEX_MARKETPLACE)
    assert data["name"] == load(REPO / ".claude-plugin" / "marketplace.json")["name"]
    assert data["interface"]["displayName"]
    (entry,) = data["plugins"]
    assert entry["name"] == "xray-cloud"
    assert entry["source"] == {"source": "local", "path": "./plugin"}
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_INSTALL"
    assert entry["category"]
    assert (REPO / entry["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()


def test_codex_manifest_paths():
    data = load(CODEX_MANIFEST)
    assert data["name"] == "xray-cloud"
    for key in ("skills", "mcpServers"):
        path = data[key]
        assert path.startswith("./"), key
        assert_relative(path)
        assert (PLUGIN / path).exists(), path
    assert {p.parent.name for p in (PLUGIN / data["skills"]).glob("*/SKILL.md")} == SKILLS


def codex_server():
    data = load(CODEX_MANIFEST)
    return load(PLUGIN / data["mcpServers"])["mcpServers"]["xray"]


def test_codex_server_command_resolves_to_main_py():
    server = codex_server()
    assert server["command"] == "python3"
    assert_relative(server["cwd"])
    cwd = PLUGIN / server["cwd"]
    (script,) = server["args"]
    assert_relative(script)
    assert (cwd / script).resolve() == SERVER_MAIN.resolve()


def test_codex_server_forwards_env_and_holds_no_values():
    server = codex_server()
    assert "env" not in server, "Codex forwards variables from the environment; no literal values"
    forwarded = set(server["env_vars"])
    assert claude_env_names() | {"XRAY_TOOLS"} <= forwarded
    assert all(re.match(r"^(XRAY|JIRA)_[A-Z_]+$", n) for n in forwarded)


# --- Cursor ----------------------------------------------------------------------------

def test_cursor_marketplace_entry():
    data = load(CURSOR_MARKETPLACE)
    assert data["name"] and data["owner"]["name"]
    names = [p["name"] for p in data["plugins"]]
    assert names == ["xray-cloud"]
    (entry,) = data["plugins"]
    assert_relative(entry["source"])
    assert (REPO / entry["source"]).resolve() == PLUGIN.resolve()
    assert (REPO / entry["source"] / ".cursor-plugin" / "plugin.json").is_file()


def test_cursor_manifest_and_skills():
    data = load(CURSOR_MANIFEST)
    assert data["name"] == "xray-cloud"
    assert data["author"]["name"]
    assert "mcpServers" not in data, "mcp.json at the plugin root is discovered automatically"
    assert "skills" not in data, "skills/ at the plugin root is discovered automatically"
    assert {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")} == SKILLS


def cursor_server():
    return load(CURSOR_MCP)["mcpServers"]["xray"]


def test_cursor_server_command_resolves_to_main_py():
    server = cursor_server()
    assert server["command"] == "python3"
    (script,) = server["args"]
    assert script.startswith("${CURSOR_PLUGIN_ROOT}/")
    assert Path(script.replace("${CURSOR_PLUGIN_ROOT}", str(PLUGIN))).resolve() == SERVER_MAIN.resolve()


def test_cursor_env_uses_declared_variables_only():
    variables = load(CURSOR_MANIFEST)["variables"]
    assert variables["type"] == "object"
    declared = set(variables["properties"])
    env = dict(cursor_server()["env"])
    # Cursor's built-in ${workspaceFolder}; if a plugin's mcp.json leaves it unexpanded, the
    # server ignores the literal placeholder and the project folder stays unknown.
    assert env.pop("XRAY_PROJECT_DIR") == "${workspaceFolder}"
    # Every value reaches the server under NAME_SETTING, never the bare NAME, so a variable
    # Cursor expands to empty cannot hide the same variable in the shell environment.
    assert all(n.endswith("_SETTING") for n in env), env
    assert claude_env_names() | {"XRAY_TOOLS"} <= {n[:-len("_SETTING")] for n in env}
    for name, value in env.items():
        assert PLACEHOLDER.fullmatch(value), (name, value)
        assert PLACEHOLDER.fullmatch(value).group(1) == name[:-len("_SETTING")]
    assert {n[:-len("_SETTING")] for n in env} == declared
    assert {"XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET"} <= set(variables.get("required", []))


def test_cursor_variables_use_allowed_keywords():
    allowed = {"type", "title", "description", "default", "enum", "const"}
    for name, schema in load(CURSOR_MANIFEST)["variables"]["properties"].items():
        assert set(schema) <= allowed, name
        assert schema["type"] == "string", name
        assert schema.get("title") and schema.get("description"), name


# --- shared ----------------------------------------------------------------------------

@pytest.mark.parametrize("path", [CODEX_MARKETPLACE, CODEX_MANIFEST, CURSOR_MARKETPLACE,
                                  CURSOR_MANIFEST, CURSOR_MCP],
                         ids=lambda p: p.relative_to(REPO).as_posix())
def test_native_manifest_has_no_version_or_secrets(path):
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "version" not in data
    for plugin in data.get("plugins", []):
        assert "version" not in plugin
    assert not re.search(r"ATATT|[0-9A-Fa-f]{32}", text)


def test_native_manifests_share_identity():
    claude = load(CLAUDE_MANIFEST)
    for path in (CODEX_MANIFEST, CURSOR_MANIFEST):
        data = load(path)
        assert data["name"] == claude["name"], path
        assert data["description"] == claude["description"].replace("from Claude Code ", ""), path
        assert data["license"] == claude["license"], path


# --- user-facing values never use the bare environment name ---------------------------

@pytest.mark.parametrize("env", [load(CLAUDE_MANIFEST)["mcpServers"]["xray"]["env"],
                                 load(CURSOR_MCP)["mcpServers"]["xray"]["env"]],
                         ids=["claude", "cursor"])
def test_manifests_never_map_a_user_value_onto_the_bare_env_name(env):
    for name, value in env.items():
        if name == "XRAY_PROJECT_DIR":
            continue
        assert name.endswith("_SETTING"), (name, value)


SERVER_READS = {"XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "XRAY_REGION", "XRAY_SETTINGS_FILE",
                "XRAY_READ_ONLY", "XRAY_TOOLS", "JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"}


@pytest.mark.parametrize("env", [load(CLAUDE_MANIFEST)["mcpServers"]["xray"]["env"],
                                 load(CURSOR_MCP)["mcpServers"]["xray"]["env"]],
                         ids=["claude", "cursor"])
def test_every_setting_name_is_one_the_server_reads(env):
    for name in env:
        if name != "XRAY_PROJECT_DIR":
            assert name[:-len("_SETTING")] in SERVER_READS, name


def test_mapped_user_config_prompts_have_no_default():
    """A default would arrive non-empty and override the environment variable."""
    manifest = load(CLAUDE_MANIFEST)
    for ref in manifest["mcpServers"]["xray"]["env"].values():
        key = ref[len("${user_config."):-1]
        assert "default" not in manifest["userConfig"][key], key


def test_cursor_variables_mapped_to_settings_have_no_default_or_enum():
    for name, schema in load(CURSOR_MANIFEST)["variables"]["properties"].items():
        assert "default" not in schema and "enum" not in schema, name


def test_jira_prompts_say_all_three_are_needed():
    claude = load(CLAUDE_MANIFEST)["userConfig"]
    cursor = load(CURSOR_MANIFEST)["variables"]["properties"]
    texts = [claude["jira_site_url"]["description"], claude["jira_email"]["description"],
             claude["jira_api_token"]["description"], cursor["JIRA_SITE_URL"]["description"],
             cursor["JIRA_EMAIL"]["description"], cursor["JIRA_API_TOKEN"]["description"]]
    for t in texts:
        low = t.lower()
        assert "email" in low and "site url" in low and "token" in low, t


# --- Icons -----------------------------------------------------------------------------

def _png_size(path):
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    assert head[12:16] == b"IHDR", path
    return struct.unpack(">II", head[16:24])


def _icon_refs():
    codex = load(CODEX_MANIFEST)["interface"]
    return [
        ("claude icon", load(CLAUDE_MANIFEST)["icon"]),
        ("codex composerIcon", codex["composerIcon"]),
        ("codex logo", codex["logo"]),
        ("cursor logo", load(CURSOR_MANIFEST)["logo"]),
    ]


@pytest.mark.parametrize("label,ref", _icon_refs())
def test_icon_is_square_png_inside_plugin(label, ref):
    assert_relative(ref)
    path = (PLUGIN / ref).resolve()
    assert PLUGIN.resolve() in path.parents, label
    assert path.is_file(), label
    width, height = _png_size(path)
    assert width == height >= 512, (label, width, height)
