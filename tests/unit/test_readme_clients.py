"""The README section for other MCP clients stays parseable and in step with the code."""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "plugin" / "server"
HEADING = "## Use with other MCP clients"


def section() -> str:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert HEADING in text, "README section missing"
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


def test_readme_json_snippets_parse():
    found = blocks("json")
    assert len(found) >= 3
    for b in found:
        json.loads(b)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib needs Python 3.11")
def test_readme_toml_snippet_parses():
    import tomllib

    found = blocks("toml")
    assert found
    for b in found:
        data = tomllib.loads(b)
        assert "xray" in data["mcp_servers"]


def test_readme_env_table_matches_code():
    table, code = table_names(), server_names()
    assert table, "env table missing"
    assert table <= code, "table names the server never reads: %s" % sorted(table - code)
    assert code <= table, "server env names missing from table: %s" % sorted(code - table)


def test_readme_has_no_secret_values():
    s = section()
    for m in re.finditer(r"(XRAY_CLIENT_SECRET|XRAY_CLIENT_ID|JIRA_API_TOKEN)\W{0,6}[:=]\s*\"?([^\s\",]+)", s):
        assert m.group(2).startswith(("${", "<")), m.group(0)
    assert not re.search(r"ATATT|[0-9A-F]{32}", s)
