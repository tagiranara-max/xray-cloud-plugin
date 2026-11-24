from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlsplit

from .config import JiraCredentials
from .errors import ToolError
from .http import RETRY_DELAYS, TRANSIENT_STATUSES, Response, Transport, is_transient_error

MAX_BODY_CHARS = 500
PAGE_SIZE = 50
MAX_PAGES = 100
# Jira link type Xray uses for requirement coverage (test "tests" requirement).
LINK_TYPE = "Test"


def normalize_site_url(raw: Any) -> str:
    """https URL with a host, no credentials, trailing slashes stripped. Never echoes the value."""
    problem = ToolError("JIRA_SITE_URL (jira_site_url setting) must be an https URL with a "
                        "host, e.g. https://yourcompany.atlassian.net")
    if not isinstance(raw, str):
        raise problem
    try:
        parts = urlsplit(raw.strip())
        host = parts.hostname
        parts.port  # noqa: B018 - validates the port
    except ValueError:
        raise problem from None
    if parts.scheme.lower() != "https" or not host or "@" in parts.netloc:
        raise problem
    return "https://" + parts.netloc.lower() + parts.path.rstrip("/")


def to_adf(text: str) -> dict:
    """Plain text -> Atlassian Document Format: one paragraph per line, empty lines kept."""
    paragraphs = []
    for line in text.splitlines() or [""]:
        node: Dict[str, Any] = {"type": "paragraph", "content": []}
        if line:
            node["content"] = [{"type": "text", "text": line}]
        paragraphs.append(node)
    return {"type": "doc", "version": 1, "content": paragraphs}


def _label(value: Any) -> Any:
    if isinstance(value, dict):
        for k in ("value", "name", "key", "id"):
            if value.get(k) is not None:
                return value[k]
    return value


class JiraClient:
    def __init__(self, creds: JiraCredentials, transport: Transport, *,
                 timeout: float = 30.0, sleep=time.sleep) -> None:
        self.site = normalize_site_url(creds.site_url)
        pair = ("%s:%s" % (creds.email, creds.token)).encode("utf-8")
        self._auth = "Basic " + base64.b64encode(pair).decode("ascii")
        self._transport = transport
        self._timeout = timeout
        self._sleep = sleep

    def __repr__(self) -> str:
        return "JiraClient(site=%r)" % self.site

    # -- plumbing ---------------------------------------------------------
    def _error(self, resp: Response, what: str) -> ToolError:
        status = resp.status
        if status == 401:
            return ToolError("Jira authentication failed for %s — check Jira email/API token"
                             % self.site, status=401)
        if status == 403:
            return ToolError("Jira denied access (HTTP 403) to %s — the API token's user "
                             "probably lacks permission for that project or issue" % what,
                             status=403)
        if status == 404:
            return ToolError("Jira returned HTTP 404 for %s — not found, or not visible to "
                             "the API token's user" % what, status=404)
        body = resp.body.decode("utf-8", errors="replace")[:MAX_BODY_CHARS]
        return ToolError("Jira request for %s failed with HTTP %d: %s" % (what, status, body),
                         status=status, body=body)

    def _request(self, method: str, path: str, what: str, *, params: Optional[dict] = None,
                 json_body: Any = None) -> Any:
        url = self.site + path
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        retry = method == "GET"  # idempotent only; see http.RETRY_DELAYS
        attempt = 0
        while True:
            try:
                resp = self._transport(method, url, headers, body, self._timeout)
            except Exception as exc:
                if retry and attempt < len(RETRY_DELAYS) and is_transient_error(exc):
                    self._sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                if isinstance(exc, ToolError):
                    raise
                raise ToolError("Network error contacting %s (%s)"
                                % (self.site, type(exc).__name__), host=self.site) from None
            if retry and resp.status in TRANSIENT_STATUSES and attempt < len(RETRY_DELAYS):
                self._sleep(RETRY_DELAYS[attempt])
                attempt += 1
                continue
            break
        if not 200 <= resp.status < 300:
            raise self._error(resp, what)
        text = resp.body.decode("utf-8", errors="replace")
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise ToolError("Jira returned a non-JSON response for %s" % what) from None

    def _paged(self, path: str, what: str, list_keys: tuple) -> List[Any]:
        out: List[Any] = []
        start = 0
        for _ in range(MAX_PAGES):
            page = self._request("GET", path, what,
                                 params={"startAt": start, "maxResults": PAGE_SIZE})
            items: List[Any] = []
            if isinstance(page, dict):
                for k in list_keys:
                    if isinstance(page.get(k), list):
                        items = page[k]
                        break
            out.extend(items)
            if not items or not isinstance(page, dict) or page.get("isLast") is True:
                break
            start += len(items)
            total = page.get("total")
            if page.get("isLast") is None and isinstance(total, int) and start >= total:
                break
        return out

    # -- operations -------------------------------------------------------
    def create_meta(self, project_key: str, issue_type: str) -> List[dict]:
        pk = quote(project_key, safe="")
        base = "/rest/api/3/issue/createmeta/%s/issuetypes" % pk
        types = self._paged(base, "issue types of project %s" % project_key,
                            ("issueTypes", "values"))
        wanted = str(issue_type).strip().lower()
        match = next((t for t in types if isinstance(t, dict)
                      and str(t.get("name", "")).lower() == wanted), None)
        if match is None:
            raise ToolError("Unknown issue type %r in project %s" % (issue_type, project_key),
                            available=[t.get("name") for t in types if isinstance(t, dict)])
        fields = self._paged("%s/%s" % (base, quote(str(match["id"]), safe="")),
                             "create fields of %s in project %s" % (match.get("name"), project_key),
                             ("fields", "values"))
        out = []
        for f in fields:
            if not isinstance(f, dict):
                continue
            out.append({
                "id": f.get("fieldId") or f.get("key"),
                "name": f.get("name"),
                "required": bool(f.get("required")),
                "schemaType": (f.get("schema") or {}).get("type"),
                "allowedValues": [_label(v) for v in (f.get("allowedValues") or [])],
            })
        return out

    def edit_issue(self, key: str, fields: dict) -> None:
        body = dict(fields)
        if isinstance(body.get("description"), str):
            body["description"] = to_adf(body["description"])
        self._request("PUT", "/rest/api/3/issue/%s" % quote(key, safe=""), "issue %s" % key,
                      json_body={"fields": body})

    def versions(self, project_key: str) -> List[dict]:
        data = self._request("GET", "/rest/api/3/project/%s/versions" % quote(project_key, safe=""),
                             "versions of project %s" % project_key)
        out = []
        for v in data if isinstance(data, list) else []:
            if isinstance(v, dict):
                out.append({"id": v.get("id"), "name": v.get("name"),
                            "released": bool(v.get("released")),
                            "archived": bool(v.get("archived")),
                            "releaseDate": v.get("releaseDate")})
        return out

    def myself(self) -> dict:
        """The Jira user the token belongs to (GET /rest/api/3/myself)."""
        data = self._request("GET", "/rest/api/3/myself", "the current user")
        return data if isinstance(data, dict) else {}

    def has_permission(self, project_key: str, permission: str) -> bool:
        """Whether the token's user holds a project permission (e.g. DELETE_ISSUES)."""
        data = self._request("GET", "/rest/api/3/mypermissions",
                             "permissions in project %s" % project_key,
                             params={"projectKey": project_key, "permissions": permission})
        entry = ((data or {}).get("permissions") or {}).get(permission) or {}
        return entry.get("havePermission") is True

    def linked_keys(self, key: str) -> List[str]:
        """Keys of issues already linked to `key` with the Test link type."""
        data = self._request("GET", "/rest/api/3/issue/%s" % quote(key, safe=""), "issue %s" % key,
                             params={"fields": "issuelinks"})
        links = ((data or {}).get("fields") or {}).get("issuelinks") or []
        out = []
        for ln in links:
            if not isinstance(ln, dict):
                continue
            link_type = ln.get("type")
            if not isinstance(link_type, dict) or link_type.get("name") != LINK_TYPE:
                continue
            other = ln.get("outwardIssue") or ln.get("inwardIssue") or {}
            if isinstance(other, dict) and other.get("key"):
                out.append(other["key"])
        return out

    def link_requirement(self, test_key: str, requirement_key: str) -> None:
        """Link a test to the requirement it covers. Verified live: POST /issueLink with the
        test as inwardIssue and the requirement as outwardIssue makes the test show
        "tests <requirement>" (and the requirement "is tested by <test>"), the same as an
        `issuelinks` add with outwardIssue = requirement on the test."""
        self._request("POST", "/rest/api/3/issueLink",
                      "link %s -> %s" % (test_key, requirement_key),
                      json_body={"type": {"name": LINK_TYPE},
                                 "inwardIssue": {"key": test_key},
                                 "outwardIssue": {"key": requirement_key}})
