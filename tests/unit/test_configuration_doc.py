"""docs/configuration.md (manual client setup) stays parseable and in step with the code, and the
README keeps one short install block per client."""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "plugin" / "server"
DOC = REPO / "docs" / "configuration.md"
HEADING = "## Other MCP clients (manual setup)"


def section() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert HEADING in text, "configuration.md section missing"
    rest = text.split(HEADING, 1)[1]
    m = re.search(r"^## ", rest, re.M)
    return rest[: m.start()] if m else rest


def blocks(lang: str):
    return re.findall(r"```%s\n(.*?)```" % lang, section(), re.S)


def table_names():
    return set(re.findall(r"^\| `((?:XRAY|JIRA)_[A-Z_]+)`", section(), re.M))


def server_names():
    names = set()
    for p in SERVER.rglob("*.py"):
        if "_vendor" in p.parts:
            continue
        names |= set(re.findall(r"[\"']((?:XRAY|JIRA)_[A-Z_]+)[\"']", p.read_text(encoding="utf-8")))
    return names


def test_config_json_snippets_parse():
    found = blocks("json")
    assert len(found) >= 3
    for b in found:
        json.loads(b)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib needs Python 3.11")
def test_config_toml_snippet_parses():
    import tomllib

    found = blocks("toml")
    assert found
    for b in found:
        data = tomllib.loads(b)
        assert "xray" in data["mcp_servers"]


def test_config_env_table_matches_code():
    table, code = table_names(), server_names()
    assert table, "env table missing"
    assert table <= code, "table names the server never reads: %s" % sorted(table - code)
    assert code <= table, "server env names missing from table: %s" % sorted(code - table)


def test_config_has_no_secret_values():
    s = section()
    for m in re.finditer(r"(XRAY_CLIENT_SECRET|XRAY_CLIENT_ID|JIRA_API_TOKEN)\W{0,6}[:=]\s*\"?([^\s\",]+)", s):
        assert m.group(2).startswith(("${", "<")), m.group(0)
    assert not re.search(r"ATATT|[0-9A-F]{32}", s)


def readme():
    return (REPO / "README.md").read_text(encoding="utf-8")


def test_readme_has_one_install_block_per_client():
    text = readme()
    install = text.split("## Install", 1)[1].split("\n## ", 1)[0]
    for client in ("Claude Code", "Codex", "Cursor", "Gemini CLI", "Other MCP clients"):
        assert "\n### %s\n" % client in install, client
    assert "/plugin install xray-cloud@xray-cloud-plugin" in install
    assert "codex plugin marketplace add" in install
    assert "plugins/local" in install
    assert "gemini extensions install" in install


def test_readme_stays_short_and_links_the_details():
    text = readme()
    assert len(text.splitlines()) < 120
    assert not table_names_in(text), "the env table belongs in docs/configuration.md"
    for doc in ("docs/configuration.md", "docs/tools.md"):
        assert "](%s" % doc in text, doc
        assert (REPO / doc).is_file(), doc


def table_names_in(text):
    return set(re.findall(r"^\| `((?:XRAY|JIRA)_[A-Z_]+)`", text, re.M))


def github_slug(heading):
    """GitHub's heading anchor: lower-case, drop punctuation except - and _, spaces to -."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path):
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    return {github_slug(h) for h in re.findall(r"^#{1,6} (.+)$", text, re.M)}


def test_github_slug():
    assert github_slug("Other MCP clients (manual setup)") == "other-mcp-clients-manual-setup"
    assert github_slug("Cursor (manual)") == "cursor-manual"
    assert github_slug("Results and native feature import/export") == \
        "results-and-native-feature-importexport"


def test_readme_links_resolve():
    links = re.findall(r"\]\(((?:docs/|CLAUDE)[^)#]+)(?:#([^)]+))?\)", readme())
    assert any(anchor for _, anchor in links)
    for target, anchor in links:
        assert (REPO / target).is_file(), target
        if anchor:
            assert anchor in anchors(REPO / target), "%s#%s" % (target, anchor)
