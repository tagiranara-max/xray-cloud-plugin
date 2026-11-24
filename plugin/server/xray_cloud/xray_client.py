from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urljoin, urlsplit

from .config import Credentials
from .errors import ToolError
from .http import (RETRY_DELAYS, TRANSIENT_STATUSES, Response, Transport, encode_multipart,
                   is_transient_error)
from .ratelimit import RateLimiter

MAX_ATTEMPTS = 3
MAX_BODY_CHARS = 500
MAX_RETRY_AFTER_S = 60.0
MAX_DOWNLOAD_REDIRECTS = 3
XRAY_HOST_SUFFIX = "xray.cloud.getxray.app"


# String literals (block and plain) and comments are blanked before looking for keywords.
_STRINGS_AND_COMMENTS = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|#[^\n]*')
_NON_QUERY_OP = re.compile(r"\b(mutation|subscription)\b")


def is_query(document: str) -> bool:
    """Whether a GraphQL document may be retried. Fail-closed: any standalone `mutation` or
    `subscription` token outside string literals and comments (at any position, e.g. after a
    fragment or a BOM) makes it non-retryable; the word inside a string argument is ignored."""
    text = _STRINGS_AND_COMMENTS.sub(" ", document.lstrip("\ufeff"))
    return not _NON_QUERY_OP.search(text)


class AuthError(ToolError):
    pass


def _header(headers: dict, name: str) -> Optional[str]:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


class XrayClient:
    def __init__(self, creds: Credentials, transport: Transport, limiter: RateLimiter,
                 *, sleep: Callable[[float], None] = time.sleep, timeout: float = 60.0) -> None:
        self._creds = creds
        self._transport = transport
        self._limiter = limiter
        self._sleep = sleep
        self._timeout = timeout
        self._token: Optional[str] = None
        self._api = creds.base_url.rstrip("/") + "/api/v2/"

    # -- auth -------------------------------------------------------------
    def _auth_error(self, detail: str) -> AuthError:
        return AuthError(
            "Xray authentication failed for region %s (%s) \u2014 check client ID/secret and region"
            % (self._creds.region, self._creds.base_url),
            region=self._creds.region, detail=detail)

    def _authenticate(self) -> str:
        self._limiter.acquire()
        request_data = json.dumps({"client_id": self._creds.client_id,
                              "client_secret": self._creds.client_secret}).encode("utf-8")
        try:
            resp = self._transport("POST", self._api + "authenticate",
                                   {"Content-Type": "application/json"}, request_data, self._timeout)
        except Exception as exc:
            raise self._auth_error("could not reach the server (%s)" % type(exc).__name__) from None
        if resp.status != 200:
            raise self._auth_error("HTTP %d" % resp.status)
        try:
            token = json.loads(_text(resp.body))
        except ValueError:
            raise self._auth_error("unexpected response format") from None
        if not isinstance(token, str) or not token:
            raise self._auth_error("unexpected response format")
        self._token = token
        return token

    # -- requests ---------------------------------------------------------
    def _send(self, method: str, url: str, headers: dict, body: Optional[bytes],
              retry: bool = False) -> Response:
        """retry=True (idempotent requests only) repeats 502/503/504 and transport resets or
        timeouts up to len(RETRY_DELAYS) times."""
        reauthed = False
        rate_attempts = 0
        transient = 0
        while True:
            self._limiter.acquire()
            token = self._token or self._authenticate()
            h = dict(headers)
            h["Authorization"] = "Bearer " + token
            try:
                resp = self._transport(method, url, h, body, self._timeout)
            except Exception as exc:
                if retry and transient < len(RETRY_DELAYS) and is_transient_error(exc):
                    self._sleep(RETRY_DELAYS[transient])
                    transient += 1
                    continue
                raise
            if retry and resp.status in TRANSIENT_STATUSES and transient < len(RETRY_DELAYS):
                self._sleep(RETRY_DELAYS[transient])
                transient += 1
                continue
            if resp.status == 401:
                self._token = None
                if reauthed:
                    raise self._auth_error("the server rejected a freshly issued token (HTTP 401)")
                reauthed = True
                continue
            if resp.status == 429:
                rate_attempts += 1
                if rate_attempts >= MAX_ATTEMPTS:
                    raise ToolError("Xray rate limit exceeded after %d attempts (HTTP 429)"
                                    % MAX_ATTEMPTS, status=429)
                self._sleep(self._backoff(resp, rate_attempts))
                continue
            return resp

    @staticmethod
    def _backoff(resp: Response, attempt: int) -> float:
        raw = _header(resp.headers, "Retry-After")
        if raw is not None:
            try:
                value = float(raw)
                if value == value and value >= 0:  # rejects NaN and negatives
                    return min(value, MAX_RETRY_AFTER_S)
            except ValueError:
                pass
        return float(2 ** (attempt - 1))

    @staticmethod
    def _http_error(resp: Response) -> ToolError:
        body = _text(resp.body)[:MAX_BODY_CHARS]
        return ToolError("Xray request failed with HTTP %d: %s" % (resp.status, body),
                         status=resp.status, body=body)

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        request_data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        resp = self._send("POST", self._api + "graphql",
                          {"Content-Type": "application/json"}, request_data, retry=is_query(query))
        if not 200 <= resp.status < 300:
            raise self._http_error(resp)
        try:
            doc = json.loads(_text(resp.body))
        except ValueError:
            raise ToolError("Xray returned a non-JSON GraphQL response",
                            status=resp.status, body=_text(resp.body)[:MAX_BODY_CHARS])
        errors: List[Any] = doc.get("errors") if isinstance(doc, dict) else None
        if errors:
            msg = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e)
                            for e in errors)
            raise ToolError(msg, graphqlErrors=errors)
        data = doc.get("data") if isinstance(doc, dict) else None
        if data is None:
            raise ToolError("GraphQL response missing data")
        return data

    def rest(self, method: str, path: str, *, json_body: Any = None,
             multipart: Optional[dict] = None, params: Optional[dict] = None,
             data: Optional[bytes] = None, content_type: Optional[str] = None) -> Any:
        """data (with content_type) sends raw bytes untouched as the request body."""
        url = self._api + path.lstrip("/")
        if params:
            url += "?" + urlencode(params, doseq=True)
        headers: Dict[str, str] = {}
        body: Optional[bytes] = None
        if multipart is not None:
            body, ctype = encode_multipart(multipart.get("fields", {}), multipart.get("files", {}))
            headers["Content-Type"] = ctype
        elif data is not None:
            body = data
            if content_type:
                headers["Content-Type"] = content_type
        elif json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        resp = self._send(method.upper(), url, headers, body, retry=method.upper() == "GET")
        if not 200 <= resp.status < 300:
            raise self._http_error(resp)
        text = _text(resp.body)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    # -- downloads --------------------------------------------------------
    def _is_xray_host(self, host: Optional[str]) -> bool:
        if not host:
            return False
        host = host.lower()
        return (host == urlsplit(self._api).hostname or host == XRAY_HOST_SUFFIX
                or host.endswith("." + XRAY_HOST_SUFFIX))

    def download(self, url: str) -> bytes:
        """GET an Xray attachment download link. The bearer token is only ever sent to Xray
        hosts over https; a redirect elsewhere (e.g. signed storage URL) is followed without it."""
        url = urljoin(self._api, url)
        parts = urlsplit(url)
        if parts.scheme != "https" or not self._is_xray_host(parts.hostname):
            raise ToolError("Refusing to download attachment from non-Xray location %s://%s"
                            % (parts.scheme, parts.hostname), host=parts.hostname)
        resp = self._send("GET", url, {}, None, retry=True)
        for _ in range(MAX_DOWNLOAD_REDIRECTS):
            location = _header(resp.headers, "Location")
            if resp.status not in (301, 302, 303, 307, 308) or not location:
                break
            url = urljoin(url, location)
            parts = urlsplit(url)
            if parts.scheme != "https":
                raise ToolError("Refusing non-https attachment redirect to %s" % parts.hostname,
                                host=parts.hostname)
            if self._is_xray_host(parts.hostname):
                resp = self._send("GET", url, {}, None, retry=True)
            else:
                self._limiter.acquire()
                resp = self._transport("GET", url, {}, None, self._timeout)
        if not 200 <= resp.status < 300:
            raise self._http_error(resp)
        return resp.body
