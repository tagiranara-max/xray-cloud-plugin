from __future__ import annotations

import socket
import urllib.error
import urllib.request
import uuid
from typing import Callable, Dict, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from .errors import ToolError


class Response(NamedTuple):
    status: int
    headers: dict
    body: bytes


Transport = Callable[[str, str, dict, Optional[bytes], float], Response]

# Retries for idempotent requests only (GET, GraphQL queries): a mutation that failed with a
# 502 or a reset may still have taken effect (seen live), so it is never repeated.
TRANSIENT_STATUSES = (502, 503, 504)
RETRY_DELAYS = (0.5, 1.5)
_TRANSIENT_EXC = (ConnectionError, TimeoutError, socket.timeout)


def _transient_exc(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        return isinstance(exc.reason, _TRANSIENT_EXC)
    return isinstance(exc, _TRANSIENT_EXC)


def is_transient_error(exc: BaseException) -> bool:
    """A connection reset/abort or timeout raised by a transport (raw, or as the ToolError
    urllib_transport makes of it)."""
    if isinstance(exc, ToolError):
        return exc.data.get("transient") is True
    return _transient_exc(exc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes],
                     timeout: float) -> Response:
    """Perform one HTTP request. Never raises on HTTP status; network errors -> ToolError."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, dict(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except Exception:
            data = b""
        return Response(exc.code, dict(exc.headers.items()) if exc.headers else {}, data)
    except Exception as exc:
        host = urlsplit(url).hostname or "unknown host"
        data = {"host": host}
        if _transient_exc(exc):
            data["transient"] = True
        raise ToolError("Network error contacting %s (%s)" % (host, type(exc).__name__), **data)


def encode_multipart(fields: Dict[str, str],
                     files: Dict[str, Tuple[str, bytes, str]]) -> Tuple[bytes, str]:
    boundary = "----xraycloud" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n'
                      % (boundary, name)).encode("utf-8") + value.encode("utf-8") + b"\r\n")
    for name, (filename, data, ctype) in files.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                      'Content-Type: %s\r\n\r\n' % (boundary, name, filename, ctype)
                      ).encode("utf-8") + data + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary
