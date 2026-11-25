"""docs/coverage.md "API parity" table and dev/parity_audit.py."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from conftest import FakeTransport, json_response

import xray_cloud.tools  # noqa: F401  (registers every tool)
from xray_cloud.config import REGIONS, Credentials
from xray_cloud.ratelimit import RateLimiter
from xray_cloud.registry import REGISTRY
from xray_cloud.xray_client import XrayClient

REPO = Path(__file__).resolve().parents[2]


def _parity_rows():
    text = (REPO / "docs" / "coverage.md").read_text(encoding="utf-8")
    assert "## API parity" in text
    section = text.split("## API parity", 1)[1]
    section = re.split(r"\n## ", section, maxsplit=1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|")]
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in rows[2:]]


def test_parity_doc_tools_exist():
    registered = {t["name"] for t in REGISTRY.list()}
    rows = _parity_rows()
    assert rows
    apis = {r[0] for r in rows}
    for needed in ("import/feature", "export/cucumber", "import/execution"):
        assert any(needed in a for a in apis), needed
    for api, cell in ((r[0], r[1]) for r in rows):
        assert cell.startswith("tool:") or cell.startswith("not exposed:"), (api, cell)
        if cell.startswith("tool:"):
            names = [n.strip().strip("`") for n in cell[len("tool:"):].split(",")]
            assert all(names), (api, cell)
            for n in names:
                assert n in registered, "%s: unknown tool %s" % (api, n)


def _load_audit():
    spec = importlib.util.spec_from_file_location("parity_audit", REPO / "dev" / "parity_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parity_audit_prints_sorted_names(capsys):
    def responder(call):
        if call.url.endswith("authenticate"):
            return json_response("tok-secret-token")
        body = json.loads(call.body)
        assert "__schema" in body["query"]
        return json_response({"data": {"__schema": {
            "mutationType": {"fields": [{"name": "zeta"}, {"name": "alpha"}]},
            "queryType": {"fields": [{"name": "getB"}, {"name": "getA"}]}}}})

    creds = Credentials("cid", "very-secret-value", "global", REGIONS["global"])
    client = XrayClient(creds, FakeTransport(responder), RateLimiter(sleep=lambda s: None),
                        sleep=lambda s: None)
    assert _load_audit().main([], client=client) == 0
    out = capsys.readouterr().out
    assert "Mutations" in out and "Queries" in out
    assert out.index("alpha") < out.index("zeta") < out.index("Queries") < out.index("getA") < out.index("getB")
    assert "very-secret-value" not in out and "tok-secret-token" not in out


def test_parity_audit_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("XRAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("XRAY_CLIENT_SECRET", raising=False)
    assert _load_audit().main([]) == 1
    assert "XRAY_CLIENT_ID" in capsys.readouterr().err


def test_tools_doc_lists_every_tool():
    doc = (REPO / "docs" / "tools.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| `(\w+)` \|", doc, re.M))
    missing = {t["name"] for t in REGISTRY.list()} - listed
    assert not missing, sorted(missing)
