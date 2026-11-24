from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xray_cloud.tools  # noqa: E402,F401  (registers every tool on REGISTRY)
from xray_cloud.config import (SettingsStore, load_credentials,  # noqa: E402
                               load_jira_credentials, load_read_only)
from xray_cloud.context import ServerContext  # noqa: E402
from xray_cloud.protocol import serve  # noqa: E402
from xray_cloud.registry import REGISTRY  # noqa: E402


def ctx_factory():
    env = os.environ
    return ServerContext(env, SettingsStore(env), load_credentials(env),
                         load_jira_credentials(env), load_read_only(env))


def _force_utf8() -> None:
    # JSON-RPC is UTF-8; don't depend on the process locale (e.g. LANG=C).
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    _force_utf8()
    serve(REGISTRY, ctx_factory)
