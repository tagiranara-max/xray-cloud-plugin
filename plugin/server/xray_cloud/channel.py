from __future__ import annotations

import json
from collections import deque
from typing import Any, Deque, Iterable, Optional, TextIO

# Sentinels returned by ClientChannel.next_message().
EOF = object()
PARSE_ERROR = object()


def is_response(msg: Any) -> bool:
    """A JSON-RPC response from the client (no method, carries result or error)."""
    return isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg)


class ClientChannel:
    """The single stdin/stdout pair shared by the serve loop and server-to-client requests.

    Single-threaded: while elicit() waits for its answer it reads the same line iterator
    as serve(). Pings are answered at once, notifications dropped, and any other message
    is queued for serve() to handle after the current response is written.
    """

    def __init__(self, stdin: Iterable[str], stdout: TextIO) -> None:
        self._lines = iter(stdin)
        self._out = stdout
        self._pending: Deque[Any] = deque()
        self._next_id = 0
        self.eof = False
        self.closed = False  # stdout is gone (broken pipe): nothing more can be sent
        self.capabilities: dict = {}
        self.current_request_id: Any = None  # id of the request serve() is handling
        self.current_cancelled = False  # the client cancelled that request: send no response

    @property
    def supports_elicitation(self) -> bool:
        return isinstance(self.capabilities, dict) and "elicitation" in self.capabilities

    def set_capabilities(self, caps: Any) -> None:
        self.capabilities = caps if isinstance(caps, dict) else {}

    def write(self, msg: dict) -> bool:
        """Send one message. False (and `closed` set) when the client has gone away."""
        if self.closed:
            return False
        try:
            self._out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._out.flush()
        except OSError:  # BrokenPipeError included
            self.closed = True
            return False
        return True

    def _drop_queued(self, request_id: Any) -> None:
        self._pending = deque(m for m in self._pending
                              if not (isinstance(m, dict) and "method" in m
                                      and m.get("id") == request_id))

    def _read(self) -> Any:
        """Next parsed message from stdin, PARSE_ERROR, or EOF. Skips blank lines."""
        if self.eof:
            return EOF
        for line in self._lines:
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except ValueError:
                return PARSE_ERROR
        self.eof = True
        return EOF

    def next_message(self) -> Any:
        """For serve(): queued messages first, then stdin. Returns EOF when done."""
        if self._pending:
            return self._pending.popleft()
        return self._read()

    def elicit(self, message: str, schema: dict) -> Optional[dict]:
        """Ask the user via `elicitation/create`. Content on accept; None otherwise.

        Decline, cancel, accept without object content, an error response (also one with
        a null id), EOF, a closed stdout, a client that never declared the elicitation
        capability, or `notifications/cancelled` for the request being handled all
        return None.
        """
        if not self.supports_elicitation or self.eof or self.closed:
            return None
        self._next_id += 1
        id_ = "xc-%d" % self._next_id
        if not self.write({"jsonrpc": "2.0", "id": id_, "method": "elicitation/create",
                           "params": {"message": message, "requestedSchema": schema}}):
            return None
        while True:
            msg = self._read()
            if msg is EOF:
                return None
            if is_response(msg):
                if "error" in msg and msg.get("id") is None:
                    return None  # the client could not even read our request's id
                if msg.get("id") != id_:
                    continue  # stray or late response: nothing waits for it
                result = msg.get("result")
                if "error" in msg or not isinstance(result, dict) \
                        or result.get("action") != "accept":
                    return None
                content = result.get("content")
                return content if isinstance(content, dict) else None
            if isinstance(msg, dict) and msg.get("method") == "ping" and "id" in msg:
                if not self.write({"jsonrpc": "2.0", "id": msg["id"], "result": {}}):
                    return None
            elif isinstance(msg, dict) and "method" in msg and "id" not in msg:
                if msg.get("method") == "notifications/cancelled":
                    params = msg.get("params")
                    rid = params.get("requestId") if isinstance(params, dict) else None
                    if rid is not None and rid == self.current_request_id:
                        self.current_cancelled = True
                        return None  # the call we are asking for was cancelled
                    if rid is not None:
                        self._drop_queued(rid)
                continue  # other notifications are dropped
            else:
                self._pending.append(msg)  # requests, parse errors, invalid messages
