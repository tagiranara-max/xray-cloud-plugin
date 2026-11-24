"""Reading local files named by the agent (step attachments, run evidence, feature files)."""
from __future__ import annotations

import os
from typing import Mapping, Tuple

from .config import project_dir
from .errors import ToolError

MAX_FILE_BYTES = 25 * 1024 * 1024


def resolve_local_path(env: Mapping[str, str], path: str) -> str:
    """~ expanded; a relative path is resolved against the project dir ($XRAY_PROJECT_DIR, else
    $CLAUDE_PROJECT_DIR, else cwd). Returns an absolute, symlink-resolved path."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("File path must be a non-empty string", path=path)
    full = os.path.expanduser(path)
    if not os.path.isabs(full):
        full = os.path.join(project_dir(env), full)
    return os.path.realpath(full)


def read_local_file(env: Mapping[str, str], path: str, what: str = "File") -> Tuple[str, bytes]:
    """-> (absolute path, bytes). Clear ToolErrors for a missing file, a directory, an
    unreadable file and a file over MAX_FILE_BYTES (25 MB)."""
    full = resolve_local_path(env, path)
    if os.path.isdir(full):
        raise ToolError("%s is a directory, not a file: %s" % (what, path), path=full)
    if not os.path.exists(full):
        raise ToolError("%s not found: %s" % (what, path), path=full)
    try:
        size = os.path.getsize(full)
    except OSError as e:
        raise ToolError("%s is not readable: %s (%s)" % (what, path, e.strerror or e), path=full)
    if size > MAX_FILE_BYTES:
        raise ToolError("%s is too large: %s is %.1f MB; the limit is %d MB"
                        % (what, path, size / (1024 * 1024), MAX_FILE_BYTES // (1024 * 1024)),
                        path=full, sizeBytes=size, maxBytes=MAX_FILE_BYTES)
    try:
        with open(full, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        raise ToolError("%s is not readable: %s (%s)" % (what, path, e.strerror or e), path=full)
    if len(data) > MAX_FILE_BYTES:
        raise ToolError("%s is too large: %s; the limit is %d MB"
                        % (what, path, MAX_FILE_BYTES // (1024 * 1024)),
                        path=full, maxBytes=MAX_FILE_BYTES)
    return full, data
