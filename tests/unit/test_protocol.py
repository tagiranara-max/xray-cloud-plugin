from __future__ import annotations

import io
import json

from xray_cloud.errors import ToolError
from xray_cloud.protocol import serve
from xray_cloud.registry import Registry


def make_registry():
    reg = Registry()

    @reg.tool("boom", "raises ToolError", {"type": "object", "properties": {}})
    def boom(ctx, args):
        raise ToolError("bad", hint="x")

    @reg.tool("crash", "raises unexpected", {"type": "object", "properties": {}})
    def crash(ctx, args):
        raise RuntimeError("kaput")

    @reg.tool("echo", "echoes", {"type": "object", "properties": {"a": {"type": "string"}}})
    def echo(ctx, args):
        return {"got": args}

    return reg


def run(messages, reg=None):
    reg = reg or make_registry()
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    serve(reg, lambda: None, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def rpc(msg):
    out = run([msg])
    assert len(out) == 1
    return out[0]


def init(version):
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}


def call_tool(name, args):
    return rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": name, "arguments": args}})["result"]


def test_initialize_echoes_supported_protocol_version():
    out = rpc(init("2025-06-18"))
    assert out["result"]["protocolVersion"] == "2025-06-18"
    assert out["result"]["capabilities"] == {"tools": {}}
    assert out["result"]["serverInfo"]["name"] == "xray-cloud"
    assert out["result"]["serverInfo"]["version"] == "1.0.0"


def test_initialize_older_supported_version_is_echoed():
    assert rpc(init("2024-11-05"))["result"]["protocolVersion"] == "2024-11-05"


def test_initialize_unknown_version_gets_latest_supported():
    assert rpc(init("1999-01-01"))["result"]["protocolVersion"] == "2025-06-18"


def test_unknown_method_returns_method_not_found():
    assert rpc({"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"] == -32601


def test_notifications_get_no_response():
    assert run([{"jsonrpc": "2.0", "method": "notifications/initialized"}]) == []


def test_ping_returns_empty_result():
    assert rpc({"jsonrpc": "2.0", "id": 5, "method": "ping"})["result"] == {}


def test_invalid_json_returns_parse_error():
    stdin = io.StringIO("not json\n")
    stdout = io.StringIO()
    serve(make_registry(), lambda: None, stdin=stdin, stdout=stdout)
    assert json.loads(stdout.getvalue())["error"]["code"] == -32700


def test_tools_list_lists_registered_tools():
    out = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    names = {t["name"] for t in out["result"]["tools"]}
    assert names == {"boom", "crash", "echo"}
    assert "inputSchema" in out["result"]["tools"][0]


def test_tool_success_wraps_json_text():
    res = call_tool("echo", {"a": "é"})
    assert "isError" not in res or res["isError"] is False
    assert json.loads(res["content"][0]["text"]) == {"got": {"a": "é"}}


def test_tool_error_becomes_is_error_result():
    res = call_tool("boom", {})
    assert res["isError"] is True
    assert json.loads(res["content"][0]["text"]) == {"error": "bad", "hint": "x"}


def test_unexpected_exception_is_reported_without_traceback_on_stdout():
    res = call_tool("crash", {})
    assert res["isError"] is True
    text = res["content"][0]["text"]
    assert "kaput" in json.loads(text)["error"]
    assert "Traceback" not in text


def test_unknown_tool_is_tool_error():
    res = call_tool("nope", {})
    assert res["isError"] is True
    assert "nope" in json.loads(res["content"][0]["text"])["error"]


def test_tool_error_from_ctx_factory_is_tool_result_and_not_cached():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ToolError("Missing credentials", hint="set XRAY_CLIENT_ID")
        return "CTX"

    msg = lambda i: {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                     "params": {"name": "echo", "arguments": {}}}
    stdin = io.StringIO(json.dumps(msg(1)) + "\n" + json.dumps(msg(2)) + "\n")
    stdout = io.StringIO()
    serve(make_registry(), factory, stdin=stdin, stdout=stdout)
    first, second = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert first["result"]["isError"] is True
    assert json.loads(first["result"]["content"][0]["text"]) == {
        "error": "Missing credentials", "hint": "set XRAY_CLIENT_ID"}
    assert "isError" not in second["result"]
    assert calls["n"] == 2
