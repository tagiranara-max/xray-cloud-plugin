from __future__ import annotations

import re
from pathlib import Path

import pytest

import xray_cloud.tools  # noqa: F401  (registers the tools)
from xray_cloud.registry import REGISTRY

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "plugin" / "skills"
TOOL_RE = re.compile(
    r"^(get|search|create|import|export|find|set|add|update|list|analyze|move|rename|delete|remove|edit)_[a-z_]+$")
EXPECTED = ["xray-setup", "import-tests", "import-results", "predict-affected-tests"]


def skill_files():
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "missing frontmatter"
    data = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip("\"'")
    return data


def test_skills_exist():
    assert skill_files()


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: p.parent.name)
def test_skill_frontmatter(path):
    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm.get("name") == path.parent.name
    assert 0 < len(fm.get("description", "")) <= 1024


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: p.parent.name)
def test_skill_tool_references_exist(path):
    registered = {t["name"] for t in REGISTRY.list()}
    for ident in re.findall(r"`([^`\s]+)`", path.read_text(encoding="utf-8")):
        if TOOL_RE.match(ident):
            assert ident in registered, "%s references unknown tool %s" % (path.parent.name, ident)


def test_all_four_skills_present():
    for name in EXPECTED:
        assert (SKILLS_DIR / name / "SKILL.md").is_file(), name


def test_write_without_settings_or_credentials_points_at_credentials_setup(tmp_path):
    from conftest import make_context
    from xray_cloud.context import NO_CREDENTIALS_MESSAGE, require_project
    from xray_cloud.errors import ToolError
    with pytest.raises(ToolError) as ei:
        require_project(make_context(tmp_path=tmp_path), "P", write=True)
    assert ei.value.message == NO_CREDENTIALS_MESSAGE


def test_xray_setup_yaml_skeleton_loads(tmp_path):
    from xray_cloud.config import SettingsStore
    text = (SKILLS_DIR / "xray-setup" / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"```yaml\n(.*?)```", text, re.S)
    assert m
    f = tmp_path / "settings.yml"
    f.write_text(m.group(1), encoding="utf-8")
    settings = SettingsStore({"XRAY_SETTINGS_FILE": str(f)}).get()
    assert settings.path is not None
    assert "PROJ" in settings.projects


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: p.parent.name)
def test_skill_text_is_client_neutral(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if "Claude Code" in line:
            assert re.search(r"\bor\b", line), "%s: Claude Code without an alternative: %s" % (path.parent.name, line)
