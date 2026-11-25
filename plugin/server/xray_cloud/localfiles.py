"""Reading local files named by the agent (step attachments, run evidence, feature files)."""
from __future__ import annotations

import os
from typing import Mapping, Tuple

from .config import project_dir
from .errors import ToolError

MAX_FILE_BYTES = 25 * 1024 * 1024

# Appended to the description of every tool parameter that takes a local path.
PATH_NOTE = ("Use an absolute path; a relative path resolves against the project folder only "
             "when the server knows it (XRAY_PROJECT_DIR, Claude Code's project, or the "
             "directory the client started the server in).")


def resolve_local_path(env: Mapping[str, str], path: str) -> str:
    """~ expanded; a relative path is resolved against the project dir ($XRAY_PROJECT_DIR, else
    $CLAUDE_PROJECT_DIR, else cwd). Returns an absolute, symlink-resolved path. When the project
    dir is unknown (server started in its own plugin folder), a relative path is refused."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("File path must be a non-empty string", path=path)
    full = os.path.expanduser(path)
    if not os.path.isabs(full):
        root = project_dir(env)
        if root is None:
            raise ToolError(
                "Relative path %r cannot be resolved: the project folder is unknown (this MCP "
                "client starts the server in its plugin folder). Pass an absolute path (or set "
                "XRAY_PROJECT_DIR)." % path,
                reason="project_dir_unknown", path=path, setting="XRAY_PROJECT_DIR")
        full = os.path.join(root, full)
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
