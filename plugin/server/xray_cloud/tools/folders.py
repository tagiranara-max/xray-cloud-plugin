"""Test Repository folder tools."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..context import (ServerContext, confirm_or_preview, dedupe, permission_guard, require_project,
                       resolve_project_id)
from ..errors import ToolError
from ..folderops import add_ids_to_folder, ensure_folder, normalize_folder_path
from ..registry import REGISTRY
from .containers import resolve_test_ids
from .tests import _LIMIT, _schema

MAX_FOLDER_FETCHES = 200

_GET_FOLDER = """
query GetFolder($projectId: String!, $path: String!) {
  getFolder(projectId: $projectId, path: $path) { name path testsCount folders }
}
"""
_TESTS_IN_FOLDER = """
query GetTestsInFolder($projectId: String!, $path: String!, $limit: Int!, $start: Int!) {
  getTests(projectId: $projectId, folder: {path: $path, includeDescendants: false}, limit: $limit, start: $start) {
    total
    results { issueId jira(fields: ["key", "summary"]) }
  }
}
"""
_RENAME = """
mutation RenameFolder($projectId: String!, $path: String!, $newName: String!) {
  renameFolder(projectId: $projectId, path: $path, newName: $newName) {
    folder { name path testsCount } warnings
  }
}
"""
_MOVE = """
mutation MoveFolder($projectId: String!, $path: String!, $destinationPath: String!) {
  moveFolder(projectId: $projectId, path: $path, destinationPath: $destinationPath) {
    folder { name path testsCount } warnings
  }
}
"""
_DELETE = """
mutation DeleteFolder($projectId: String!, $path: String!) {
  deleteFolder(projectId: $projectId, path: $path)
}
"""

_ID = {"type": "string", "description": "Optional numeric Xray project id; it is always resolved "
                                        "from projectKey and, when given, must match it"}
_PATH = {"type": "string", "description": "Folder path, e.g. /Orders/Returns"}


def _pid(ctx: ServerContext, args: dict) -> str:
    """The project id resolved from projectKey (the allowlisted key). A numericProjectId passed
    by the caller must equal it, so an id cannot redirect writes to another project."""
    resolved = resolve_project_id(ctx, args["projectKey"])
    given = args.get("numericProjectId")
    if given is not None and str(given).strip() != resolved:
        raise ToolError("numericProjectId %s does not belong to project %s"
                        % (given, args["projectKey"]),
                        numericProjectId=given, projectKey=args["projectKey"])
    return resolved


def _folder_schema(props: dict, required: List[str]) -> dict:
    p = dict(props)
    p["numericProjectId"] = _ID
    return _schema(p, required)


def get_folder(ctx: ServerContext, project_id: str, path: str, fresh: bool = False) -> dict:
    """getFolder with the 5-minute cache. Returns {name, path, testsCount, folders}."""
    ck = (project_id, path)
    hit = None if fresh else ctx.folder_cache.get(ck)
    if hit is not None:
        return hit
    data = ctx.xray.graphql(_GET_FOLDER, {"projectId": project_id, "path": path})
    folder = data.get("getFolder")
    if not folder:
        raise ToolError("Folder not found: %s" % path, path=path)
    ctx.folder_cache.set(ck, folder)
    return folder


def _warnings(res: dict) -> List[Any]:
    return [w for w in (res.get("warnings") or []) if w]


@REGISTRY.tool("create_folder",
               "Create a Test Repository folder (and any missing parents). Idempotent: an "
               "existing folder is not an error.",
               _folder_schema({"path": _PATH}, ["path"]), write=True)
def create_folder(ctx, args):
    require_project(ctx, args["projectKey"], write=True)
    path = normalize_folder_path(args["path"], allow_root=False)
    ensure_folder(ctx, _pid(ctx, args), path)
    return {"path": path, "created": True}


@REGISTRY.tool("add_tests_to_folder",
               "Move tests into a folder (created if missing). Tests must belong to the project.",
               _folder_schema({"path": _PATH, "testKeys": {"type": "array", "minItems": 1,
                                                           "items": {"type": "string"}}},
                              ["path", "testKeys"]), write=True)
def add_tests_to_folder(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=True)
    path = normalize_folder_path(args["path"], allow_root=False)
    keys = dedupe(args["testKeys"])
    if not keys:
        raise ToolError("testKeys must not be empty")
    ids = resolve_test_ids(ctx, pk, keys)
    project_id = _pid(ctx, args)
    ensure_folder(ctx, project_id, path)
    res = add_ids_to_folder(ctx, project_id, path, ids)
    out: Dict[str, Any] = {"path": path, "added": keys}
    if _warnings(res):
        out["warnings"] = _warnings(res)
    return out


@REGISTRY.tool("get_tests_in_folder",
               "List the tests directly inside a folder (paginated with limit and start).",
               _folder_schema({"path": _PATH, "limit": _LIMIT,
                               "start": {"type": "integer", "minimum": 0}}, ["path"]))
def get_tests_in_folder(ctx, args):
    require_project(ctx, args["projectKey"], write=False)
    path = normalize_folder_path(args["path"])
    start, limit = args.get("start", 0), args.get("limit", 100)
    data = ctx.xray.graphql(_TESTS_IN_FOLDER, {"projectId": _pid(ctx, args), "path": path,
                                               "limit": limit, "start": start})
    r = data.get("getTests") or {}
    rows = [{"key": (t.get("jira") or {}).get("key"),
             "summary": (t.get("jira") or {}).get("summary")} for t in r.get("results") or []]
    return {"path": path, "total": r.get("total", len(rows)), "start": start, "tests": rows}


def _node(f: dict) -> dict:
    return {"name": f.get("name"), "path": f.get("path"), "testsCount": f.get("testsCount"),
            "children": []}


@REGISTRY.tool("list_folders",
               "List the folder tree below a path (default: the repository root).",
               _folder_schema({"path": _PATH}, []))
def list_folders(ctx, args):
    require_project(ctx, args["projectKey"], write=False)
    root = normalize_folder_path(args.get("path", "/"))
    project_id = _pid(ctx, args)
    nodes: Dict[str, dict] = {}
    queue = [root]
    fetched = 0
    while queue:
        path = queue[0]
        if fetched >= MAX_FOLDER_FETCHES:
            break
        try:
            f = get_folder(ctx, project_id, path)
        except ToolError:
            if path == root:
                raise
            break  # e.g. rate limited: return what we have
        queue.pop(0)
        fetched += 1
        node = nodes.setdefault(path, _node(f))
        node.update(name=f.get("name"), testsCount=f.get("testsCount"))
        for c in f.get("folders") or []:
            cp = c.get("path")
            if not cp or cp in nodes:
                continue
            child = nodes[cp] = _node(c)
            node["children"].append(child)
            queue.append(cp)
    out: Dict[str, Any] = {"tree": nodes.get(root) or _node({"name": root, "path": root}),
                           "complete": not queue}
    if queue:
        out["pendingPaths"] = queue
    return out


def _check_new_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or "/" in name or name.strip() in (".", ".."):
        raise ToolError("newName must be a single folder name without '/'", newName=name)
    return name.strip()


@REGISTRY.tool("rename_folder", "Rename a folder (its path changes, its tests stay).",
               _folder_schema({"path": _PATH, "newName": {"type": "string"}}, ["path", "newName"]),
               write=True)
def rename_folder(ctx, args):
    require_project(ctx, args["projectKey"], write=True)
    path = normalize_folder_path(args["path"], allow_root=False)
    new_name = _check_new_name(args["newName"])
    ctx.folder_cache.clear()
    try:
        res = (ctx.xray.graphql(_RENAME, {"projectId": _pid(ctx, args), "path": path,
                                          "newName": new_name}).get("renameFolder") or {})
    finally:
        ctx.folder_cache.clear()
    out: Dict[str, Any] = {"path": path, "newName": new_name, "renamed": True}
    if _warnings(res):
        out["warnings"] = _warnings(res)
    return out


@REGISTRY.tool("move_folder",
               "Move a folder (with its contents) under another folder; destinationPath is the "
               "target parent folder ('/' for the root).",
               _folder_schema({"path": _PATH, "destinationPath": _PATH},
                              ["path", "destinationPath"]), write=True)
def move_folder(ctx, args):
    require_project(ctx, args["projectKey"], write=True)
    path = normalize_folder_path(args["path"], allow_root=False)
    dest = normalize_folder_path(args["destinationPath"])
    if dest == path or dest.startswith(path + "/"):
        raise ToolError("Cannot move a folder into itself or its own subfolder",
                        path=path, destinationPath=dest)
    ctx.folder_cache.clear()
    try:
        res = (ctx.xray.graphql(_MOVE, {"projectId": _pid(ctx, args), "path": path,
                                        "destinationPath": dest}).get("moveFolder") or {})
    finally:
        ctx.folder_cache.clear()
    out: Dict[str, Any] = {"path": path, "destinationPath": dest, "moved": True}
    if _warnings(res):
        out["warnings"] = _warnings(res)
    return out


@REGISTRY.tool("delete_folder",
               "Delete a folder and its subfolders (tests are kept, only unfiled). Without "
               "confirm: true it only previews the folder and its test count.",
               _folder_schema({"path": _PATH, "confirm": {"type": "boolean"}}, ["path"]),
               write=True)
def delete_folder(ctx, args):
    require_project(ctx, args["projectKey"], write=True)
    path = normalize_folder_path(args["path"], allow_root=False)
    project_id = _pid(ctx, args)
    f = get_folder(ctx, project_id, path, fresh=True)
    preview = confirm_or_preview(args.get("confirm", False),
                                 {"path": path, "testCount": f.get("testsCount"),
                                  "subfolders": len(f.get("folders") or [])})
    if preview is not None:
        return preview
    try:
        with permission_guard("delete folder %s" % path, args["projectKey"]):
            ctx.xray.graphql(_DELETE, {"projectId": project_id, "path": path})
    finally:
        ctx.folder_cache.clear()
    return {"path": path, "deleted": True}
