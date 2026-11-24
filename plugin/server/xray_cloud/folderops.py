"""Folder helpers shared by create_test (folderPath) and the folder tools."""
from __future__ import annotations

from typing import List

from .context import ServerContext
from .errors import ToolError

_CREATE_FOLDER = """
mutation CreateFolder($projectId: String!, $path: String!) {
  createFolder(projectId: $projectId, path: $path) { folder { name path testsCount } warnings }
}
"""
_ADD_TO_FOLDER = """
mutation AddTestsToFolder($projectId: String!, $path: String!, $testIssueIds: [String!]!) {
  addTestsToFolder(projectId: $projectId, path: $path, testIssueIds: $testIssueIds) {
    folder { name path testsCount } warnings
  }
}
"""


def normalize_folder_path(path: str, allow_root: bool = True) -> str:
    """Leading '/', no trailing '/', '//' collapsed. '..' / '.' segments are rejected."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("Folder path must be a non-empty string", path=path)
    segments = [s.strip() for s in path.strip().split("/")]
    segments = [s for s in segments if s]
    if any(s in ("..", ".") for s in segments):
        raise ToolError("Folder path must not contain '.' or '..' segments", path=path)
    if not segments:
        if allow_root:
            return "/"
        raise ToolError("The root folder '/' is not allowed here", path=path)
    return "/" + "/".join(segments)


def ensure_folder(ctx: ServerContext, project_id: str, path: str) -> None:
    """createFolder, treating 'already exists' as success."""
    try:
        ctx.xray.graphql(_CREATE_FOLDER, {"projectId": project_id, "path": path})
    except ToolError as e:
        if "already exist" not in e.message.lower():
            raise
    finally:
        ctx.folder_cache.clear()


def add_ids_to_folder(ctx: ServerContext, project_id: str, path: str,
                      issue_ids: List[str]) -> dict:
    try:
        data = ctx.xray.graphql(_ADD_TO_FOLDER, {"projectId": project_id, "path": path,
                                                 "testIssueIds": issue_ids})
    finally:
        ctx.folder_cache.clear()
    return data.get("addTestsToFolder") or {}
