from __future__ import annotations

from xray_cloud.protocol import SUPPORTED_VERSIONS

from test_protocol import run, rpc


def _init(version):
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}


def test_unknown_protocol_version_gets_server_version():
    out = rpc(_init("2025-11-25"))
    assert out["result"]["protocolVersion"] == SUPPORTED_VERSIONS[0]


def test_notification_gets_no_response():
    assert run([{"jsonrpc": "2.0", "method": "notifications/initialized"}]) == []


def test_unknown_method_is_32601():
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
    out = run(msgs)
    assert [o["id"] for o in out] == [1, 2, 3]
    assert out[0]["error"]["code"] == -32601
    assert out[1]["error"]["code"] == -32601
    assert out[2]["result"] == {}  # still serving


def test_elicitation_capability_leaves_initialize_result_unchanged():
    msg = _init("2025-06-18")
    msg["params"]["capabilities"] = {"elicitation": {}}
    out = rpc(msg)
    assert out["result"]["protocolVersion"] == "2025-06-18"
    assert out["result"]["capabilities"] == {"tools": {}}


def test_client_response_without_pending_request_gets_no_reply():
    msgs = [{"jsonrpc": "2.0", "id": "xc-1", "result": {"action": "decline"}},
            {"jsonrpc": "2.0", "id": "xc-2", "error": {"code": -1, "message": "x"}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
    assert [o["id"] for o in run(msgs)] == [3]
