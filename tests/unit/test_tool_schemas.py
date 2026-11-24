from __future__ import annotations

import re

import xray_cloud.tools  # noqa: F401  (registers every tool)
from xray_cloud.registry import REGISTRY

NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def test_every_tool_schema_is_portable():
    tools = REGISTRY.list()
    assert tools
    problems = []
    for t in tools:
        n, schema = t["name"], t["inputSchema"]
        if not NAME.match(n):
            problems.append("%s: bad name" % n)
        if not t["description"] or len(t["description"]) > 1024:
            problems.append("%s: description length %d" % (n, len(t["description"] or "")))
        if schema.get("type") != "object":
            problems.append("%s: input schema not an object" % n)
        for k in ("anyOf", "oneOf", "allOf", "not"):
            if k in schema:
                problems.append("%s: top-level %s" % (n, k))
        for p, spec in (schema.get("properties") or {}).items():
            if "type" not in spec and "enum" not in spec:
                problems.append("%s.%s: no type or enum" % (n, p))
    assert not problems, problems
