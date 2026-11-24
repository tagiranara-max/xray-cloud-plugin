import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "server"))


from typing import Callable, List, NamedTuple, Optional, Union  # noqa: E402

from xray_cloud.http import Response  # noqa: E402


class Call(NamedTuple):
    method: str
    url: str
    headers: dict
    body: Optional[bytes]


class FakeTransport:
    """Records calls; returns queued Responses (or raises queued exceptions) or uses a responder."""

    def __init__(self, responder: Optional[Callable[[Call], Response]] = None) -> None:
        self.calls: List[Call] = []
        self.queue: List[Union[Response, Exception]] = []
        self.responder = responder

    def push(self, *items) -> "FakeTransport":
        self.queue.extend(items)
        return self

    def __call__(self, method, url, headers, body, timeout):
        call = Call(method, url, dict(headers), body)
        self.calls.append(call)
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self.responder:
            return self.responder(call)
        raise AssertionError("FakeTransport: no response queued for %s %s" % (method, url))


def json_response(obj, status=200, headers=None) -> Response:
    import json
    return Response(status, headers or {}, json.dumps(obj).encode("utf-8"))


import re  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Keep ~/.config/xray-cloud/settings.* of the developer out of every test."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


class FakeXray:
    """Stands in for XrayClient: maps GraphQL operation name -> canned response
    (dict, or callable(variables) -> dict). Records (op, variables) in .calls."""

    def __init__(self, responses: Optional[dict] = None) -> None:
        self.responses = dict(responses or {})
        self.calls: List[tuple] = []
        self.rest_calls: List[tuple] = []
        self.rest_responses: dict = {}  # (METHOD, path) -> response or callable(kwargs)
        self.downloads: dict = {}  # url -> bytes, or an Exception to raise
        self.download_calls: List[str] = []

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        m = re.search(r"\b(?:query|mutation)\s+(\w+)", query)
        op = m.group(1) if m else ""
        self.calls.append((op, variables or {}))
        if op not in self.responses:
            raise AssertionError("FakeXray: no response for operation %r" % op)
        resp = self.responses[op]
        return resp(variables or {}) if callable(resp) else resp

    def rest(self, method, path, **kwargs):
        self.rest_calls.append((method, path, kwargs))
        key = (method.upper(), path)
        if key not in self.rest_responses:
            raise AssertionError("FakeXray: unexpected REST call %s %s" % (method, path))
        resp = self.rest_responses[key]
        return resp(kwargs) if callable(resp) else resp

    def download(self, url: str) -> bytes:
        self.download_calls.append(url)
        if url not in self.downloads:
            raise AssertionError("FakeXray: unexpected download %s" % url)
        item = self.downloads[url]
        if isinstance(item, Exception):
            raise item
        return item

    def calls_of(self, op: str) -> List[dict]:
        return [v for o, v in self.calls if o == op]


def make_context(settings_yaml: str = "", read_only: bool = False, jira: bool = False,
                 responses: Optional[dict] = None, tmp_path=None):
    """ServerContext wired with FakeXray (ctx.xray is the FakeXray). A settings file is
    written under a temp dir only when settings_yaml is non-empty; otherwise no file
    is findable (CLAUDE_PROJECT_DIR is the temp dir, HOME is isolated). jira=True sets jira_creds (ctx.jira is then a real JiraClient, built lazily)."""
    import tempfile
    from xray_cloud.config import JiraCredentials, SettingsStore
    from xray_cloud.context import ServerContext

    root = Path(tmp_path) if tmp_path is not None else Path(tempfile.mkdtemp())
    env = {"HOME": str(root / "home"), "CLAUDE_PROJECT_DIR": str(root)}
    if settings_yaml:
        f = root / "settings.yml"
        f.write_text(settings_yaml)
        env["XRAY_SETTINGS_FILE"] = str(f)
    fake = FakeXray(responses)
    jc = JiraCredentials("https://x.atlassian.net", "a@b.c", "tok") if jira else None
    ctx = ServerContext(env, SettingsStore(env), None, jc, read_only, xray=fake)
    return ctx
