from __future__ import annotations

import base64
import json

import pytest

from conftest import FakeTransport, json_response
from xray_cloud.config import JiraCredentials
from xray_cloud.context import ServerContext
from xray_cloud.config import SettingsStore
from xray_cloud.errors import ToolError
from xray_cloud.http import Response
from xray_cloud.jira_client import JiraClient, normalize_site_url, to_adf

TOKEN = "jira-tok-SECRET-123"
EMAIL = "me@example.com"
SITE = "https://acme.atlassian.net"


def make(fake, site=SITE):
    return JiraClient(JiraCredentials(site, EMAIL, TOKEN), fake)


def assert_no_secret(*things):
    pair = base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
    for t in things:
        s = t if isinstance(t, str) else json.dumps(t, default=str)
        assert TOKEN not in s and pair not in s and "Basic " not in s


def test_basic_auth_header_and_url():
    fake = FakeTransport().push(json_response([]))
    make(fake, SITE + "/").versions("P")
    c = fake.calls[0]
    expect = "Basic " + base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
    assert c.headers["Authorization"] == expect
    assert c.url == SITE + "/rest/api/3/project/P/versions" and c.method == "GET"


@pytest.mark.parametrize("raw", ["http://a.atlassian.net", "acme.atlassian.net", "https://",
                                 "ftp://x.com", "https://u:pw@acme.atlassian.net", "", None,
                                 "https://acme.atlassian.net:abc"])
def test_site_url_rejected_without_echo(raw):
    with pytest.raises(ToolError) as ei:
        normalize_site_url(raw)
    assert "JIRA_SITE_URL" in str(ei.value) and "pw" not in str(ei.value)


def test_site_url_normalized():
    assert normalize_site_url(" https://Acme.atlassian.net// ") == SITE


def test_repr_hides_secrets():
    jc = JiraCredentials(SITE, EMAIL, TOKEN)
    assert_no_secret(repr(jc), repr(make(FakeTransport())))
    assert EMAIL not in repr(jc)


def test_to_adf_paragraph_per_line_and_empty_paragraphs():
    doc = to_adf("a\n\nb")
    assert doc["type"] == "doc" and doc["version"] == 1
    assert doc["content"] == [
        {"type": "paragraph", "content": [{"type": "text", "text": "a"}]},
        {"type": "paragraph", "content": []},
        {"type": "paragraph", "content": [{"type": "text", "text": "b"}]}]


def test_edit_issue_put_with_adf_description():
    fake = FakeTransport().push(Response(204, {}, b""))
    make(fake).edit_issue("P-1", {"summary": "s", "description": "x\ny", "labels": ["a"]})
    c = fake.calls[0]
    assert c.method == "PUT" and c.url == SITE + "/rest/api/3/issue/P-1"
    body = json.loads(c.body)["fields"]
    assert body["summary"] == "s" and body["labels"] == ["a"]
    assert body["description"]["content"][1]["content"][0]["text"] == "y"


def _types():
    return {"issueTypes": [{"id": "1", "name": "Bug"}, {"id": "10", "name": "Test"}],
            "isLast": True}


def test_create_meta_resolves_type_case_insensitively_and_parses():
    fields = {"fields": [
        {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string"}},
        {"fieldId": "customfield_1", "name": "Severity", "required": False,
         "schema": {"type": "option"}, "allowedValues": [{"id": "5", "value": "High"},
                                                          {"id": "6", "value": "Low"}]},
        {"fieldId": "fixVersions", "name": "Fix versions", "required": False,
         "schema": {"type": "array"}, "allowedValues": [{"id": "9", "name": "1.0"}]}],
        "isLast": True}
    fake = FakeTransport().push(json_response(_types()), json_response(fields))
    out = make(fake).create_meta("P", "test")
    assert fake.calls[0].url.startswith(SITE + "/rest/api/3/issue/createmeta/P/issuetypes?")
    assert fake.calls[1].url.startswith(SITE + "/rest/api/3/issue/createmeta/P/issuetypes/10?")
    assert out[0] == {"id": "summary", "name": "Summary", "required": True,
                      "schemaType": "string", "allowedValues": []}
    assert out[1]["allowedValues"] == ["High", "Low"] and out[1]["schemaType"] == "option"
    assert out[2]["allowedValues"] == ["1.0"]


def test_create_meta_paginates_by_total():
    p1 = {"issueTypes": [{"id": "1", "name": "Bug"}], "startAt": 0, "total": 2}
    p2 = {"issueTypes": [{"id": "10", "name": "Test"}], "startAt": 1, "total": 2}
    fake = FakeTransport().push(json_response(p1), json_response(p2),
                                json_response({"fields": [], "isLast": True}))
    make(fake).create_meta("P", "Test")
    assert "startAt=1" in fake.calls[1].url and fake.calls[2].url.count("issuetypes/10") == 1


def test_create_meta_unknown_type_lists_available():
    fake = FakeTransport().push(json_response(_types()))
    with pytest.raises(ToolError) as ei:
        make(fake).create_meta("P", "Epic")
    assert ei.value.data["available"] == ["Bug", "Test"]


def test_versions_parsed():
    fake = FakeTransport().push(json_response([{"id": "1", "name": "1.0", "released": True,
                                                "extra": 1}]))
    assert make(fake).versions("P") == [{"id": "1", "name": "1.0", "released": True,
                                         "archived": False, "releaseDate": None}]


def test_link_requirement_body():
    fake = FakeTransport().push(Response(201, {}, b""))
    make(fake).link_requirement("P-1", "R-2")
    assert fake.calls[0].url == SITE + "/rest/api/3/issueLink"
    assert json.loads(fake.calls[0].body) == {"type": {"name": "Test"},
                                              "inwardIssue": {"key": "P-1"},
                                              "outwardIssue": {"key": "R-2"}}


def test_linked_keys():
    fake = FakeTransport().push(json_response({"fields": {"issuelinks": [
        {"type": {"name": "Test"}, "outwardIssue": {"key": "R-1"}},
        {"type": {"name": "Blocks"}, "outwardIssue": {"key": "R-9"}}]}}))
    assert make(fake).linked_keys("P-1") == ["R-1"]


def test_linked_keys_ignores_malformed_link_types():
    fake = FakeTransport().push(json_response({"fields": {"issuelinks": [
        {"type": "Test", "outwardIssue": {"key": "R-2"}},
        {"type": ["Test"], "outwardIssue": {"key": "R-3"}},
        {"type": None, "outwardIssue": {"key": "R-4"}},
        {"type": {"name": "Test"}, "inwardIssue": {"key": "R-1"}}]}}))
    assert make(fake).linked_keys("P-1") == ["R-1"]


@pytest.mark.parametrize("status,needle", [(401, "Jira authentication failed for %s" % SITE),
                                           (403, "lacks permission"),
                                           (404, "404"), (500, "HTTP 500")])
def test_errors_never_leak_token(status, needle):
    fake = FakeTransport().push(Response(status, {}, b'{"errorMessages":["boom"]}'))
    with pytest.raises(ToolError) as ei:
        make(fake).edit_issue("P-1", {"summary": "s"})
    assert needle in ei.value.message
    assert_no_secret(ei.value.message, ei.value.to_dict(), repr(ei.value))


def test_401_message_text():
    fake = FakeTransport().push(Response(401, {}, b""))
    with pytest.raises(ToolError) as ei:
        make(fake).versions("P")
    assert ei.value.message == ("Jira authentication failed for %s — check Jira email/API "
                                "token" % SITE)


def test_network_error_never_leaks_token():
    fake = FakeTransport().push(OSError("connect failed Basic " + TOKEN))
    with pytest.raises(ToolError) as ei:
        make(fake).versions("P")
    assert "acme.atlassian.net" in ei.value.message
    assert_no_secret(ei.value.message, ei.value.to_dict())


def test_context_builds_jira_lazily():
    env = {}
    jc = JiraCredentials(SITE, EMAIL, TOKEN)
    ctx = ServerContext(env, SettingsStore(env), None, jc, transport=FakeTransport())
    assert isinstance(ctx.jira, JiraClient) and ctx.jira is ctx.jira
    assert ServerContext(env, SettingsStore(env), None).jira is None
    ctx.jira = None
    assert ctx.jira is None


def test_context_bad_site_url_is_tool_error():
    env = {}
    ctx = ServerContext(env, SettingsStore(env), None, JiraCredentials("http://x", EMAIL, TOKEN))
    with pytest.raises(ToolError, match="JIRA_SITE_URL"):
        ctx.jira


def test_get_retried_on_transient_failures_post_not():
    sleeps = []
    fake = FakeTransport().push(Response(502, {}, b""), ConnectionResetError("reset"),
                                json_response([]))
    jc = JiraClient(JiraCredentials(SITE, EMAIL, TOKEN), fake, sleep=sleeps.append)
    assert jc.versions("P") == [] and sleeps == [0.5, 1.5]
    fake.push(Response(503, {}, b""))
    with pytest.raises(ToolError) as ei:
        jc.link_requirement("P-1", "R-1")
    assert ei.value.data["status"] == 503 and sleeps == [0.5, 1.5]


def test_get_retries_exhausted_keep_clear_error():
    sleeps = []
    fake = FakeTransport().push(*[ConnectionResetError("Basic " + TOKEN)] * 3)
    jc = JiraClient(JiraCredentials(SITE, EMAIL, TOKEN), fake, sleep=sleeps.append)
    with pytest.raises(ToolError) as ei:
        jc.versions("P")
    assert ei.value.message.startswith("Network error contacting") and sleeps == [0.5, 1.5]
    assert_no_secret(ei.value.message, ei.value.to_dict())
