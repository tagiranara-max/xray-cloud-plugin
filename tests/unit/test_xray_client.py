from __future__ import annotations

import json

import pytest

from conftest import FakeTransport, json_response
from xray_cloud.config import REGIONS, Credentials
from xray_cloud.errors import ToolError
from xray_cloud.http import Response, encode_multipart, urllib_transport
from xray_cloud.ratelimit import RateLimiter
from xray_cloud.xray_client import AuthError, XrayClient

SECRET = "s3cr3t-value-XYZ"
TOKEN = "tok-abc.def.ghi"


def auth_ok():
    return json_response(TOKEN)


def make(fake, region="global"):
    creds = Credentials("cid", SECRET, region, REGIONS[region])
    sleeps = []
    c = XrayClient(creds, fake, RateLimiter(sleep=lambda s: None), sleep=sleeps.append)
    c.sleeps = sleeps
    return c


@pytest.fixture
def fake():
    return FakeTransport()


def test_authenticates_against_regional_url(fake):
    fake.push(auth_ok(), json_response({"data": {"x": 1}}))
    assert make(fake, "eu").graphql("{x}") == {"x": 1}
    a, g = fake.calls
    assert a.url == "https://eu.xray.cloud.getxray.app/api/v2/authenticate"
    assert json.loads(a.body) == {"client_id": "cid", "client_secret": SECRET}
    assert g.url == "https://eu.xray.cloud.getxray.app/api/v2/graphql"
    assert g.headers["Authorization"] == "Bearer " + TOKEN


def test_token_cached_between_calls(fake):
    fake.push(auth_ok(), json_response({"data": {}}), json_response({"data": {}}))
    c = make(fake)
    c.graphql("{a}")
    c.graphql("{b}")
    assert [x.url.endswith("authenticate") for x in fake.calls] == [True, False, False]


def test_401_reauthenticates_once_then_fails(fake):
    fake.push(auth_ok(), Response(401, {}, b""), auth_ok(), json_response({"data": {"ok": 1}}))
    assert make(fake).graphql("{x}") == {"ok": 1}
    fake2 = FakeTransport().push(auth_ok(), Response(401, {}, b""), auth_ok(), Response(401, {}, b""))
    with pytest.raises(AuthError):
        make(fake2).graphql("{x}")
    assert len(fake2.calls) == 4


def test_429_retries_with_retry_after(fake):
    fake.push(auth_ok(), Response(429, {"retry-after": "7"}, b""), json_response({"data": {"a": 1}}))
    c = make(fake)
    assert c.graphql("{x}") == {"a": 1}
    assert c.sleeps == [7.0]


def test_429_fallback_backoff_and_cap(fake):
    fake.push(auth_ok(), Response(429, {"Retry-After": "soon"}, b""), Response(429, {}, b""),
              Response(429, {}, b""))
    c = make(fake)
    with pytest.raises(ToolError) as e:
        c.graphql("{x}")
    assert e.value.data["status"] == 429
    assert c.sleeps == [1.0, 2.0]
    assert len(fake.calls) == 4  # auth + 3 attempts


def test_auth_failure_message_names_region_not_secret(fake):
    fake.push(Response(401, {}, ('{"error":"%s"}' % SECRET).encode()))
    with pytest.raises(AuthError) as e:
        make(fake, "eu").graphql("{x}")
    assert str(e.value) == ("Xray authentication failed for region eu "
                            "(https://eu.xray.cloud.getxray.app) \u2014 check client ID/secret and region")
    assert SECRET not in str(e.value) and SECRET not in repr(e.value.data)


@pytest.mark.parametrize("resp", [
    Response(200, {}, ("not json " + SECRET).encode()),
    Response(500, {}, ("boom " + SECRET + " " + TOKEN).encode()),
    ToolError("Network error contacting host (URLError)"),
    OSError("connection failed " + SECRET),
])
def test_auth_failures_never_leak_secret_or_token(resp):
    c = make(FakeTransport().push(resp))
    with pytest.raises(ToolError) as e:
        c.graphql("{x}")
    for text in (str(e.value), repr(e.value), repr(e.value.data)):
        assert SECRET not in text and TOKEN not in text


@pytest.mark.parametrize("raw,expected", [("3600", 60.0), ("-5", 1.0), ("nan", 1.0), ("1e999", 60.0)])
def test_retry_after_capped_and_sanitised(raw, expected):
    fake = FakeTransport().push(auth_ok(), Response(429, {"Retry-After": raw}, b""),
                                json_response({"data": {}}))
    c = make(fake)
    c.graphql("{x}")
    assert c.sleeps == [expected]


def test_http_error_truncated_body(fake):
    fake.push(auth_ok(), Response(500, {}, b"x" * 2000))
    with pytest.raises(ToolError) as e:
        make(fake).graphql("{x}")
    assert e.value.data["status"] == 500 and len(e.value.data["body"]) == 500
    assert TOKEN not in str(e.value)


def test_graphql_errors_become_tool_error(fake):
    errs = [{"message": "bad one"}, {"message": "bad two"}]
    fake.push(auth_ok(), json_response({"errors": errs}))
    with pytest.raises(ToolError) as e:
        make(fake).graphql("{x}")
    assert str(e.value) == "bad one; bad two" and e.value.data["graphqlErrors"] == errs


def test_rest_params_json_and_text(fake):
    fake.push(auth_ok(), Response(200, {}, b"Feature: x"), json_response([1]), Response(204, {}, b""))
    c = make(fake)
    assert c.rest("GET", "export/cucumber", params={"keys": "A-1;A-2"}) == "Feature: x"
    assert fake.calls[1].url == "https://xray.cloud.getxray.app/api/v2/export/cucumber?keys=A-1%3BA-2"
    assert c.rest("POST", "import/test", json_body={"a": 1}) == [1]
    assert json.loads(fake.calls[2].body) == {"a": 1}
    assert c.rest("DELETE", "x") is None


def test_rest_multipart_sent(fake):
    fake.push(auth_ok(), json_response({"ok": True}))
    make(fake).rest("POST", "import/feature", multipart={
        "fields": {"projectKey": "P"}, "files": {"file": ("a.feature", b"F", "text/plain")}})
    assert fake.calls[1].headers["Content-Type"].startswith("multipart/form-data; boundary=")


def test_multipart_roundtrip():
    body, ctype = encode_multipart({"projectKey": "PROJ"},
                                   {"file": ("a.feature", "Feature: é".encode(), "text/plain")})
    boundary = ctype.split("boundary=")[1]
    assert body.startswith(("--" + boundary).encode())
    assert body.endswith(("--" + boundary + "--\r\n").encode())
    assert b'name="projectKey"\r\n\r\nPROJ\r\n' in body
    assert b'name="file"; filename="a.feature"\r\nContent-Type: text/plain\r\n\r\nFeature: \xc3\xa9\r\n' in body


def test_urllib_transport_network_error_names_host():
    with pytest.raises(ToolError) as e:
        urllib_transport("GET", "http://127.0.0.1:1/x?secret=1", {}, None, 2)
    assert "127.0.0.1" in str(e.value) and "secret" not in str(e.value)


# -- download ----------------------------------------------------------------

def test_download_sends_bearer_to_xray_host_and_returns_bytes(fake):
    fake.push(auth_ok(), Response(200, {}, b"\x89PNG"))
    assert make(fake).download("https://xray.cloud.getxray.app/api/v2/attachments/a1") == b"\x89PNG"
    g = fake.calls[1]
    assert g.method == "GET" and g.headers["Authorization"] == "Bearer " + TOKEN


def test_download_resolves_relative_link_against_api(fake):
    fake.push(auth_ok(), Response(200, {}, b"x"))
    make(fake, "eu").download("attachments/a1")
    assert fake.calls[1].url == "https://eu.xray.cloud.getxray.app/api/v2/attachments/a1"


def test_download_refuses_foreign_host_without_any_request(fake):
    with pytest.raises(ToolError) as e:
        make(fake).download("https://evil.example.com/a1")
    assert "evil.example.com" in e.value.message
    assert fake.calls == []


def test_download_refuses_plain_http(fake):
    with pytest.raises(ToolError):
        make(fake).download("http://xray.cloud.getxray.app/api/v2/attachments/a1")
    assert fake.calls == []


def test_download_follows_redirect_to_storage_without_token(fake):
    fake.push(auth_ok(), Response(302, {"Location": "https://bucket.s3.amazonaws.com/f?sig=1"}, b""),
              Response(200, {}, b"data"))
    assert make(fake).download("https://xray.cloud.getxray.app/api/v2/attachments/a1") == b"data"
    hop = fake.calls[2]
    assert hop.url == "https://bucket.s3.amazonaws.com/f?sig=1"
    assert "Authorization" not in hop.headers


def test_download_http_error_never_contains_token_or_secret(fake):
    fake.push(auth_ok(), Response(404, {}, b"not found"))
    with pytest.raises(ToolError) as e:
        make(fake).download("https://xray.cloud.getxray.app/api/v2/attachments/a1")
    text = json.dumps(e.value.to_dict())
    assert TOKEN not in text and SECRET not in text and "404" in text


# -- transient retries (idempotent requests only) -------------------------------------------

BAD_GATEWAY = Response(502, {}, b"<html>502 Bad Gateway</html>")


def test_query_retried_on_502_then_succeeds(fake):
    fake.push(auth_ok(), BAD_GATEWAY, Response(503, {}, b""), json_response({"data": {"x": 1}}))
    c = make(fake)
    assert c.graphql("query Q { x }") == {"x": 1}
    assert c.sleeps == [0.5, 1.5] and len(fake.calls) == 4


def test_shorthand_query_retried_on_connection_reset(fake):
    fake.push(auth_ok(), ConnectionResetError("reset"),
              ToolError("Network error contacting h (ConnectionResetError)", transient=True),
              json_response({"data": {"x": 1}}))
    c = make(fake)
    assert c.graphql("{ x }") == {"x": 1} and c.sleeps == [0.5, 1.5]


@pytest.mark.parametrize("failure", [BAD_GATEWAY, ConnectionResetError("reset")])
def test_mutation_never_retried(fake, failure):
    fake.push(auth_ok(), failure)
    c = make(fake)
    with pytest.raises(Exception):
        c.graphql("\n  # comment\n  mutation M { deleteTest(issueId: \"1\") }")
    assert c.sleeps == [] and len(fake.calls) == 2


def test_post_rest_never_retried(fake):
    fake.push(auth_ok(), BAD_GATEWAY)
    c = make(fake)
    with pytest.raises(ToolError):
        c.rest("POST", "import/feature", params={"projectKey": "P"})
    assert c.sleeps == []


def test_get_rest_and_download_retried(fake):
    fake.push(auth_ok(), BAD_GATEWAY, json_response({"ok": True}))
    c = make(fake)
    assert c.rest("GET", "export/x") == {"ok": True} and c.sleeps == [0.5]
    fake.push(Response(504, {}, b""), Response(200, {}, b"bytes"))
    assert c.download("attachments/a1") == b"bytes" and c.sleeps == [0.5, 0.5]


def test_retries_exhausted_give_the_usual_error(fake):
    fake.push(auth_ok(), BAD_GATEWAY, BAD_GATEWAY, BAD_GATEWAY)
    c = make(fake)
    with pytest.raises(ToolError) as e:
        c.graphql("query Q { x }")
    assert e.value.message.startswith("Xray request failed with HTTP 502")
    assert e.value.data["status"] == 502 and c.sleeps == [0.5, 1.5] and len(fake.calls) == 4
    fake.push(*[ToolError("Network error contacting h (ConnectionResetError)", transient=True)] * 3)
    with pytest.raises(ToolError) as e:
        c.graphql("query Q { x }")
    assert e.value.message == "Network error contacting h (ConnectionResetError)"


def test_non_transient_errors_not_retried(fake):
    fake.push(auth_ok(), Response(500, {}, b"boom"))
    c = make(fake)
    with pytest.raises(ToolError):
        c.graphql("query Q { x }")
    fake.push(ToolError("Network error contacting h (URLError)"))
    with pytest.raises(ToolError):
        c.graphql("query Q { x }")
    assert c.sleeps == []


def test_retry_goes_through_rate_limiter(fake):
    acquired = []

    class Limiter:
        def acquire(self):
            acquired.append(1)

    fake.push(auth_ok(), BAD_GATEWAY, json_response({"data": {}}))
    c = XrayClient(Credentials("cid", SECRET, "global", REGIONS["global"]), fake, Limiter(),
                   sleep=lambda s: None)
    c.graphql("query Q { x }")
    assert len(acquired) == 3  # auth + two attempts


def test_urllib_transport_marks_resets_transient(monkeypatch):
    import urllib.request

    class Opener:
        def __init__(self, exc):
            self.exc = exc

        def open(self, req, timeout):
            raise self.exc

    import socket
    import urllib.error
    for exc, transient in ((ConnectionResetError("x"), True), (socket.timeout("t"), True),
                           (urllib.error.URLError(ConnectionResetError("x")), True),
                           (urllib.error.URLError("dns"), False)):
        monkeypatch.setattr(urllib.request, "build_opener", lambda *a, e=exc: Opener(e))
        with pytest.raises(ToolError) as ei:
            urllib_transport("GET", "https://h.example/x", {}, None, 1)
        assert ei.value.data.get("transient", False) is transient


@pytest.mark.parametrize("doc,retryable", [
    ("query Q { x }", True),
    ("{ x }", True),
    ("# mutation in a comment\nquery Q { x }", True),
    ('query Q { getTests(jql: "summary ~ \\"mutation\\"", limit: 1) { total } }', True),
    ('query Q { a(s: """block mutation text""") }', True),
    ("mutation M { deleteTest(issueId: \"1\") }", False),
    ("﻿mutation M { deleteTest(issueId: \"1\") }", False),
    ("fragment F on Test { issueId }\nmutation M { deleteTest(issueId: \"1\") { ...F } }", False),
    ("# just a comment\n  mutation M { x }", False),
    ("subscription S { x }", False),
    ('query Q { a(s: "unterminated mutation) }', False),
])
def test_is_query_fail_closed(doc, retryable):
    # Words inside string literals and comments are ignored; any other `mutation` /
    # `subscription` token makes the document non-retryable.
    from xray_cloud.xray_client import is_query
    assert is_query(doc) is retryable


@pytest.mark.parametrize("doc", [
    "fragment F on Test { issueId }\nmutation M { deleteTest(issueId: \"1\") }",
    "﻿mutation M { deleteTest(issueId: \"1\") }"])
def test_fragment_or_bom_mutation_not_retried(fake, doc):
    fake.push(auth_ok(), BAD_GATEWAY)
    c = make(fake)
    with pytest.raises(ToolError):
        c.graphql(doc)
    assert c.sleeps == [] and len(fake.calls) == 2


def test_rest_raw_data_sent_untouched(fake):
    fake.push(auth_ok(), json_response({"ok": True}))
    raw = "<testsuites>\xe9</testsuites>".encode("latin-1")
    make(fake).rest("POST", "import/execution/junit", params={"projectKey": "P"},
                    data=raw, content_type="text/xml")
    call = fake.calls[1]
    assert call.body == raw and call.headers["Content-Type"] == "text/xml"
    assert call.url.endswith("import/execution/junit?projectKey=P")
