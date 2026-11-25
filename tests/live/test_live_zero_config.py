"""Live checks of zero-config write approval over the real stdio server: no settings file,
no Jira token, a temp XRAY_DATA_DIR. Asking by needs_answer (client without elicitation) and
by `elicitation/create` (client that declares it); decline; revoke; an unknown project.

Everything created carries the live RUN_ID in its summary; keys are tracked in the session
`live` fixture, which deletes them (and sweeps by RUN_ID) at session end.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional

import pytest

from .conftest import LIVE_LABEL, Live

MAIN = Path(__file__).resolve().parents[2] / "plugin" / "server" / "main.py"
_KEYS = ("XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "XRAY_REGION")
_SECRETS = ("XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "JIRA_API_TOKEN")
TIMEOUT = 120


class StdioServer:
    """The server as a subprocess, driven message by message (so it can ask us things)."""

    def __init__(self, tmp: Path, elicitation: bool) -> None:
        for d in ("home", "data", "cwd"):
            (tmp / d).mkdir(parents=True, exist_ok=True)
        self.data = tmp / "data"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp / "home"), "XRAY_DATA_DIR": str(self.data)}
        env.update({k: os.environ[k] for k in _KEYS if (os.environ.get(k) or "").strip()})
        self.proc = subprocess.Popen([sys.executable, str(MAIN)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env=env, cwd=str(tmp / "cwd"))
        self.out: "queue.Queue[Optional[dict]]" = queue.Queue()
        self.stdout_lines: List[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        self.elicitations: List[dict] = []
        self._id = 0
        caps = {"elicitation": {}} if elicitation else {}
        init = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": caps,
                                           "clientInfo": {"name": "live", "version": "0"}})
        assert "result" in init, init
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.stdout_lines.append(line)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # not a protocol message (stray output): kept in stdout_lines only
            self.out.put(msg)
        self.out.put(None)

    def send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict,
                on_elicit: Optional[Callable[[dict], dict]] = None) -> dict:
        self._id += 1
        my_id = self._id
        self.send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
        while True:
            msg = self.out.get(timeout=TIMEOUT)
            assert msg is not None, "server exited: %s" % self.stderr_tail()
            if msg.get("method") == "elicitation/create":
                self.elicitations.append(msg)
                assert on_elicit is not None, "unexpected elicitation: %s" % msg
                self.send({"jsonrpc": "2.0", "id": msg["id"], "result": on_elicit(msg)})
                continue
            if msg.get("id") == my_id:
                return msg

    def tool(self, name: str, args: dict, on_elicit=None) -> dict:
        resp = self.request("tools/call", {"name": name, "arguments": args}, on_elicit)
        assert "result" in resp, resp
        body = json.loads(resp["result"]["content"][0]["text"])
        body["_isError"] = bool(resp["result"].get("isError"))
        return body

    def stderr_tail(self) -> str:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return (self.proc.stderr.read() or "")[-500:]
        except (OSError, ValueError):
            return ""

    def close(self) -> str:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        err = self.proc.stderr.read() or ""
        out = "".join(self.stdout_lines)
        for k in _SECRETS:
            v = (os.environ.get(k) or "").strip()
            if v:
                assert v not in out and v not in err, "a secret appeared in the server output"
        return err

    def preferences(self) -> str:
        p = self.data / "preferences.json"
        return p.read_text(encoding="utf-8") if p.exists() else ""


@pytest.fixture
def server_factory(tmp_path):
    servers: List[StdioServer] = []

    def make(elicitation: bool, sub: str = "s") -> StdioServer:
        srv = StdioServer(tmp_path / ("%s%d" % (sub, len(servers))), elicitation)
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.close()


def _create(srv: StdioServer, lv: Live, text: str, on_elicit=None, **extra) -> dict:
    args = {"projectKey": lv.project, "summary": lv.summary(text), "testType": "Manual",
            "rawFields": {"labels": [LIVE_LABEL]}, "aiLabel": "", "allowDuplicates": True}
    args.update(extra)
    res = srv.tool("create_test", args, on_elicit)
    if res.get("key") and res.get("created") is True:
        lv.track("test", res["key"])
    return res


def _delete(srv: StdioServer, lv: Live, key: str, on_elicit=None, **extra) -> None:
    res = srv.tool("delete_test", dict({"projectKey": lv.project, "testKey": key,
                                        "confirm": True}, **extra), on_elicit)
    assert not res["_isError"] and res.get("deleted") is True, res
    lv.untrack(key)


def _approvals(srv: StdioServer) -> dict:
    st = srv.tool("get_status", {})
    assert not st["_isError"], st
    return st["approvals"]


def _answer(value: Optional[str], action: str = "accept"):
    def handler(msg: dict) -> dict:
        props = msg["params"]["requestedSchema"]["properties"]["approve"]
        assert props["enum"] == ["all", "project", "no"] and props["default"] == "all"
        assert msg["params"]["message"]  # the question text
        if action != "accept":
            return {"action": action}
        return {"action": "accept", "content": {"approve": value}}
    return handler


def test_needs_answer_then_allow_project(wlive, server_factory):
    srv = server_factory(elicitation=False)
    first = _create(srv, wlive, "zero-config needs_answer")
    assert first.get("status") == "needs_answer", first
    assert [q["id"] for q in first["questions"]] == ["allowProject"]
    assert not first.get("key")
    assert "approvedProjects" not in srv.preferences()  # nothing remembered yet
    created = _create(srv, wlive, "zero-config needs_answer", allowProject="project")
    assert created.get("created") is True and created.get("key"), created
    # existence check accepted the existing project; only this project is remembered
    ap = _approvals(srv)
    assert ap == {"approveAll": False, "approvedProjects": [wlive.project]}, ap
    _delete(srv, wlive, created["key"])  # approved: no question this time
    assert srv.elicitations == []


def test_elicit_approve_all_then_no_question(wlive, server_factory):
    srv = server_factory(elicitation=True)
    # the agent's allowProject never bypasses the user's answer when the client can elicit
    first = _create(srv, wlive, "zero-config elicit 1", on_elicit=_answer("all"),
                    allowProject="project")
    assert first.get("created") is True and first.get("key"), first
    assert len(srv.elicitations) == 1
    assert srv.elicitations[0]["id"].startswith("xc-")
    assert _approvals(srv)["approveAll"] is True
    second = _create(srv, wlive, "zero-config elicit 2")  # on_elicit=None: asking would fail
    assert second.get("created") is True and second.get("key"), second
    assert len(srv.elicitations) == 1
    _delete(srv, wlive, first["key"])
    _delete(srv, wlive, second["key"])
    revoke = srv.tool("set_preferences", {"projectKey": wlive.project, "approveAll": False})
    assert not revoke["_isError"], revoke
    assert _approvals(srv) == {"approveAll": False, "approvedProjects": []}


@pytest.mark.parametrize("answer", [("no", "accept"), (None, "decline"), (None, "cancel")])
def test_elicit_decline_writes_nothing(wlive, server_factory, answer):
    srv = server_factory(elicitation=True)
    res = _create(srv, wlive, "zero-config declined", on_elicit=_answer(*answer))
    assert res["_isError"] and "not approved" in res.get("error", json.dumps(res)), res
    assert not res.get("key")
    assert len(srv.elicitations) == 1
    assert _approvals(srv) == {"approveAll": False, "approvedProjects": []}
    assert wlive.project not in srv.preferences()  # nothing remembered
    found = srv.tool("search_tests", {"projectKey": wlive.project,
                                      "jql": 'project = %s AND summary ~ "%s"'
                                             % (wlive.project, wlive.run_id + " zero-config declined")})
    assert not found["_isError"], found
    hits = [t for t in found.get("tests") or found.get("results") or []
            if "declined" in (t.get("summary") or "")]
    assert hits == [], hits


def test_revoke_project_asks_again(wlive, server_factory):
    srv = server_factory(elicitation=True)
    first = _create(srv, wlive, "zero-config revoke 1", on_elicit=_answer("project"))
    assert first.get("created") is True, first
    assert _approvals(srv) == {"approveAll": False, "approvedProjects": [wlive.project]}
    revoke = srv.tool("set_preferences", {"projectKey": wlive.project,
                                          "revokeProject": wlive.project})
    assert not revoke["_isError"], revoke
    assert _approvals(srv) == {"approveAll": False, "approvedProjects": []}
    # the next write (the delete) asks again; decline it, then approve it
    refused = srv.tool("delete_test", {"projectKey": wlive.project, "testKey": first["key"],
                                       "confirm": True}, _answer("no"))
    assert refused["_isError"], refused
    assert len(srv.elicitations) == 2
    _delete(srv, wlive, first["key"], _answer("project"))
    assert len(srv.elicitations) == 3


def test_revoke_project_needs_answer_again(wlive, server_factory):
    srv = server_factory(elicitation=False)
    first = _create(srv, wlive, "zero-config revoke na", allowProject="project")
    assert first.get("created") is True, first
    revoke = srv.tool("set_preferences", {"projectKey": wlive.project,
                                          "revokeProject": wlive.project})
    assert not revoke["_isError"], revoke
    again = srv.tool("delete_test", {"projectKey": wlive.project, "testKey": first["key"],
                                     "confirm": True})
    assert again.get("status") == "needs_answer", again
    _delete(srv, wlive, first["key"], allowProject="project")


def test_unknown_project_is_not_remembered(wlive, server_factory):
    srv = server_factory(elicitation=False)
    pk = "ZZXQNOPE"
    res = srv.tool("create_test", {"projectKey": pk, "summary": wlive.summary("zero-config nope"),
                                   "testType": "Manual", "aiLabel": "",
                                   "allowProject": "project"})
    assert res["_isError"] and "not found" in res.get("error", ""), res
    assert _approvals(srv) == {"approveAll": False, "approvedProjects": []}
