from __future__ import annotations

import io
import json

import pytest

import xray_cloud.tools  # noqa: F401  (registers every tool)
from xray_cloud import toolgroups
from xray_cloud.config import SettingsError
from xray_cloud.protocol import serve
from xray_cloud.registry import REGISTRY, Registry


def test_groups_cover_every_registered_tool_exactly_once():
    flat = [t for names in toolgroups.GROUPS.values() for t in names]
    assert len(flat) == len(set(flat))
    assert set(flat) == set(REGISTRY._tools)


def test_unset_or_empty_means_all():
    assert toolgroups.enabled_tools({}) is None
    assert toolgroups.enabled_tools({"XRAY_TOOLS": ""}) is None
    assert toolgroups.enabled_tools({"XRAY_TOOLS": "  "}) is None
    assert toolgroups.enabled_tools({"XRAY_TOOLS": "${user_config.tools}"}) is None


def test_values_are_case_and_whitespace_tolerant_and_setup_always_on():
    got = toolgroups.enabled_tools({"XRAY_TOOLS": " Analysis , FOLDERS "})
    expected = set()
    for g in ("analysis", "folders", "setup"):
        expected |= set(toolgroups.GROUPS[g])
    assert got == expected


def _run(env, messages):
    reg = Registry()
    reg._tools = dict(REGISTRY._tools)
    reg.allowed = toolgroups.enabled_tools(env)
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    serve(reg, lambda: None, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_filter_limits_tools_list_and_call():
    out = _run({"XRAY_TOOLS": "analysis"}, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "create_test", "arguments": {}}}])
    names = {t["name"] for t in out[0]["result"]["tools"]}
    assert names == set(toolgroups.GROUPS["analysis"]) | set(toolgroups.GROUPS["setup"])
    assert out[1]["result"]["isError"] is True
    assert "XRAY_TOOLS" in out[1]["result"]["content"][0]["text"]


def test_unknown_group_error_lists_valid_groups():
    with pytest.raises(SettingsError) as exc:
        toolgroups.enabled_tools({"XRAY_TOOLS": "tests,bogus"})
    msg = str(exc.value)
    assert "bogus" in msg
    for g in toolgroups.GROUPS:
        assert g in msg
