from __future__ import annotations

import io
import json
from types import SimpleNamespace

from xray_cloud.channel import ClientChannel
from xray_cloud.protocol import serve
from xray_cloud.registry import Registry

SCHEMA = {"type": "object", "properties": {"choice": {"type": "string", "enum": ["a", "b"]}}}


def _init(caps):
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": caps,
                       "clientInfo": {"name": "t", "version": "0"}}}


def _call(id_, name="ask"):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": name, "arguments": {}}}


def _answer(result, id_="xc-1"):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _registry(seen):
    reg = Registry()

    @reg.tool("ask", "elicits", {"type": "object", "properties": {}})
    def ask(ctx, args):
        got = ctx.channel.elicit("Pick one", SCHEMA)
        seen.append(got)
        return {"got": got}

    @reg.tool("echo", "echoes", {"type": "object", "properties": {}})
    def echo(ctx, args):
        return {"echo": True}

    return reg


def _serve(messages, seen=None):
    seen = [] if seen is None else seen
    lines = [m if isinstance(m, str) else json.dumps(m) + "\n" for m in messages]
    stdout = io.StringIO()
    ctx = SimpleNamespace(channel=None)
    serve(_registry(seen), lambda: ctx, stdin=lines, stdout=stdout)
    return [json.loads(l) for l in stdout.getvalue().splitlines()], seen, ctx


def _tool_json(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def test_capabilities_recorded_from_initialize():
    out, _, ctx = _serve([_init({"elicitation": {}}), _call(2), _answer({"action": "decline"})])
    assert isinstance(ctx.channel, ClientChannel)
    assert ctx.channel.supports_elicitation is True
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"


def test_no_elicitation_capability_means_unsupported():
    out, seen, ctx = _serve([_init({}), _call(2)])
    assert ctx.channel.supports_elicitation is False
    assert seen == [None]
    # nothing was sent to the client: only the two responses
    assert [o.get("id") for o in out] == [1, 2]


def test_elicit_accept_returns_content():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2),
                           _answer({"action": "accept", "content": {"choice": "a"}})])
    req = out[1]
    assert req == {"jsonrpc": "2.0", "id": "xc-1", "method": "elicitation/create",
                   "params": {"message": "Pick one", "requestedSchema": SCHEMA}}
    assert seen == [{"choice": "a"}]
    assert out[2]["id"] == 2


def test_elicit_decline_and_cancel_return_none():
    for result in ({"action": "decline"}, {"action": "cancel"}):
        _, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _answer(result)])
        assert seen == [None]
    # an error response is a decline too
    err = {"jsonrpc": "2.0", "id": "xc-1", "error": {"code": -1, "message": "no"}}
    _, seen, _ = _serve([_init({"elicitation": {}}), _call(2), err])
    assert seen == [None]


def test_ids_increase_per_elicitation():
    out, seen, _ = _serve([_init({"elicitation": {}}),
                           _call(2), _answer({"action": "accept", "content": {"choice": "a"}}),
                           _call(3), _answer({"action": "accept", "content": {"choice": "b"}}, "xc-2")])
    assert seen == [{"choice": "a"}, {"choice": "b"}]
    assert [o.get("id") for o in out] == [1, "xc-1", 2, "xc-2", 3]


def test_ping_during_elicitation_answered():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2),
                           {"jsonrpc": "2.0", "id": 9, "method": "ping"},
                           {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
                           _answer({"action": "accept", "content": {"choice": "b"}})])
    assert [o.get("id") for o in out] == [1, "xc-1", 9, 2]
    assert out[2]["result"] == {}
    assert seen == [{"choice": "b"}]


def test_stray_response_during_elicitation_ignored():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2),
                           _answer({"action": "accept", "content": {"choice": "a"}}, "xc-99"),
                           _answer({"action": "decline"})])
    assert seen == [None]
    assert [o.get("id") for o in out] == [1, "xc-1", 2]


def test_request_during_elicitation_queued_and_answered_after():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2),
                           _call(3, "echo"),
                           {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                           "not json\n",
                           _answer({"action": "accept", "content": {"choice": "a"}}),
                           {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
    assert [o.get("id") for o in out] == [1, "xc-1", 2, 3, 4, None, 5]
    assert _tool_json(out[2]) == {"got": {"choice": "a"}}
    assert _tool_json(out[3]) == {"echo": True}
    assert "tools" in out[4]["result"]
    assert out[5]["error"]["code"] == -32700


def test_eof_during_elicitation_is_decline():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2)])
    assert seen == [None]
    assert [o.get("id") for o in out] == [1, "xc-1", 2]


def test_eof_after_queued_request_still_answers_it():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _call(3, "echo")])
    assert seen == [None]
    assert [o.get("id") for o in out] == [1, "xc-1", 2, 3]


def test_client_response_outside_elicitation_is_ignored():
    out, _, _ = _serve([_init({}), _answer({"action": "accept"}, "xc-7"),
                        {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert [o.get("id") for o in out] == [1, 2]


def test_server_context_channel_defaults_to_none():
    from xray_cloud.context import ServerContext
    from xray_cloud.config import SettingsStore
    env = {"HOME": "/nonexistent"}
    ctx = ServerContext(env, SettingsStore(env), None)
    assert ctx.channel is None


def _cancel(request_id):
    return {"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": request_id, "reason": "user"}}


def test_cancel_of_in_flight_call_ends_elicitation():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _cancel(2),
                           {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
    assert seen == [None]
    # elicit returned at the cancel; no response for the cancelled call; still serving
    assert [o.get("id") for o in out] == [1, "xc-1", 5]


def test_cancel_of_other_id_is_dropped_while_waiting():
    _, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _cancel(77),
                         _answer({"action": "accept", "content": {"choice": "a"}})])
    assert seen == [{"choice": "a"}]


def test_cancel_of_queued_request_removes_it():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _call(3, "echo"),
                           {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, _cancel(3),
                           _answer({"action": "decline"})])
    assert seen == [None]
    assert [o.get("id") for o in out] == [1, "xc-1", 2, 4]


def test_accept_without_dict_content_is_none():
    for result in ({"action": "accept"}, {"action": "accept", "content": "yes"}):
        _, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _answer(result)])
        assert seen == [None]


def test_error_with_null_id_fails_pending_elicitation():
    err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2), err,
                           _answer({"action": "accept", "content": {"choice": "a"}})])
    assert seen == [None]  # the error ended the wait; the later answer is a stray
    assert [o.get("id") for o in out] == [1, "xc-1", 2]


def test_queued_request_that_elicits_runs_after_current():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _call(3),
                           _answer({"action": "accept", "content": {"choice": "a"}}),
                           _answer({"action": "accept", "content": {"choice": "b"}}, "xc-2")])
    assert seen == [{"choice": "a"}, {"choice": "b"}]
    assert [o.get("id") for o in out] == [1, "xc-1", 2, "xc-2", 3]


def test_ping_answered_then_eof_with_requests_queued():
    out, seen, _ = _serve([_init({"elicitation": {}}), _call(2), _call(3, "echo"), _call(4),
                           {"jsonrpc": "2.0", "id": 9, "method": "ping"}])
    # ping at once; EOF declines call 2; queued 3 is answered; queued 4 elicits nothing after EOF
    assert [o.get("id") for o in out] == [1, "xc-1", 9, 2, 3, 4]
    assert seen == [None, None]


class _BrokenOut:
    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass


def test_broken_pipe_ends_serve_quietly(capsys):
    lines = [json.dumps(_init({})) + "\n", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"]
    serve(_registry([]), lambda: SimpleNamespace(channel=None), stdin=lines, stdout=_BrokenOut())
    assert "Traceback" not in capsys.readouterr().err


def test_broken_pipe_during_elicit_returns_none():
    ch = ClientChannel([], _BrokenOut())
    ch.set_capabilities({"elicitation": {}})
    assert ch.elicit("q", SCHEMA) is None
    assert ch.closed is True
