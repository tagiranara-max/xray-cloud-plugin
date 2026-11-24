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
        self.capabilities: dict = {}

    @property
    def supports_elicitation(self) -> bool:
        return isinstance(self.capabilities, dict) and "elicitation" in self.capabilities

    def set_capabilities(self, caps: Any) -> None:
        self.capabilities = caps if isinstance(caps, dict) else {}

    def write(self, msg: dict) -> None:
        self._out.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._out.flush()

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

        Decline, cancel, an error response, EOF, or a client that never declared the
        elicitation capability all return None.
        """
        if not self.supports_elicitation or self.eof:
            return None
        self._next_id += 1
        id_ = "xc-%d" % self._next_id
        self.write({"jsonrpc": "2.0", "id": id_, "method": "elicitation/create",
                    "params": {"message": message, "requestedSchema": schema}})
        while True:
            msg = self._read()
            if msg is EOF:
                return None
            if is_response(msg):
                if msg.get("id") != id_:
                    continue  # stray or late response: nothing waits for it
                result = msg.get("result")
                if "error" in msg or not isinstance(result, dict) or result.get("action") != "accept":
                    return None
                content = result.get("content")
                return content if isinstance(content, dict) else {}
            if isinstance(msg, dict) and msg.get("method") == "ping" and "id" in msg:
                self.write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            elif isinstance(msg, dict) and "method" in msg and "id" not in msg:
                continue  # notification
            else:
                self._pending.append(msg)  # requests, parse errors, invalid messages
