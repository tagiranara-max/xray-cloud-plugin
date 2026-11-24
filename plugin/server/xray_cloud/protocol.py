from __future__ import annotations

import json
import sys
from typing import Any, Callable, Optional

from .errors import ToolError
from .registry import Registry, _error_result

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "xray-cloud", "version": "1.0.0"}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _handle(registry: Registry, ctx_factory: Callable[[], Any], state: dict, msg: Any) -> Optional[dict]:
    if not isinstance(msg, dict) or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method = msg["method"]
    if "id" not in msg:  # notification
        return None
    id_ = msg["id"]
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": registry.list()}
    elif method == "tools/call":
        if "ctx" not in state:  # lazy: credential problems surface on first call
            try:
                state["ctx"] = ctx_factory()
            except ToolError as e:  # not cached: the next call retries
                return {"jsonrpc": "2.0", "id": id_, "result": _error_result(e.to_dict())}
            except Exception as e:
                print("context creation failed: %r" % (e,), file=sys.stderr)
                return {"jsonrpc": "2.0", "id": id_,
                        "result": _error_result({"error": "Server context failed: %s" % e})}
        result = registry.call(state["ctx"], params.get("name"), params.get("arguments") or {})
    else:
        return _error(id_, -32601, "Method not found: %s" % method)
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def serve(registry: Registry, ctx_factory: Callable[[], Any], stdin=None, stdout=None) -> None:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    state: dict = {}
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = _error(None, -32700, "Parse error")
        else:
            try:
                resp = _handle(registry, ctx_factory, state, msg)
            except Exception as e:
                print("protocol error: %r" % (e,), file=sys.stderr)
                resp = _error(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
