#!/usr/bin/env python3
"""Dev-only, read-only: list the Xray Cloud GraphQL mutations and queries (schema introspection).

Compare the output with the "API parity" table in docs/coverage.md. Needs live credentials in
the environment (XRAY_CLIENT_ID, XRAY_CLIENT_SECRET, optional XRAY_REGION); never prints them.

    python3 dev/parity_audit.py
"""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin", "server"))

from xray_cloud.config import SettingsError, load_credentials  # noqa: E402
from xray_cloud.errors import ToolError  # noqa: E402
from xray_cloud.http import urllib_transport  # noqa: E402
from xray_cloud.ratelimit import RateLimiter  # noqa: E402
from xray_cloud.xray_client import XrayClient  # noqa: E402

QUERY = ("query Schema { __schema { mutationType { fields { name } } "
         "queryType { fields { name } } } }")


def _names(type_info: Any) -> List[str]:
    fields = (type_info or {}).get("fields") or []
    return sorted(f["name"] for f in fields)


def main(argv: Optional[List[str]] = None, client: Optional[XrayClient] = None) -> int:
    try:
        if client is None:
            creds = load_credentials(os.environ)
            if creds is None:
                print("Set XRAY_CLIENT_ID and XRAY_CLIENT_SECRET (and XRAY_REGION if not global).",
                      file=sys.stderr)
                return 1
            client = XrayClient(creds, urllib_transport, RateLimiter())
        schema = client.graphql(QUERY)["__schema"]
    except (SettingsError, ToolError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    mutations, queries = _names(schema.get("mutationType")), _names(schema.get("queryType"))
    print("Mutations (%d)" % len(mutations))
    for name in mutations:
        print("  " + name)
    print("")
    print("Queries (%d)" % len(queries))
    for name in queries:
        print("  " + name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
