from __future__ import annotations

import json

import pytest

from conftest import make_context
from xray_cloud.errors import ToolError
from xray_cloud.folderops import normalize_folder_path
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = "projects:\n  P: {}\n"


def _ctx(*a, **kw):
    ctx = make_context(*a, **kw)
    ctx.project_ids["P"] = "1"  # resolved project id for projectKey P
    return ctx


def call(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def folder(path="/a", count=3, children=()):
    return {"getFolder": {"name": path.rsplit("/", 1)[-1] or "/", "path": path,
                          "testsCount": count, "folders": list(children)}}


@pytest.mark.parametrize("raw,want", [("a/b", "/a/b"), ("/a/b/", "/a/b"), ("//a//b//", "/a/b"),
                                      (" /a ", "/a"), ("/", "/")])
def test_normalize(raw, want):
    assert normalize_folder_path(raw) == want


def test_normalize_rejects_dotdot_and_root_when_disallowed():
    for bad in ("/a/../b", "..", "/./a"):
        with pytest.raises(ToolError):
            normalize_folder_path(bad)
    with pytest.raises(ToolError):
        normalize_folder_path("//", allow_root=False)


def test_project_id_resolved_from_key():
    ctx = make_context(YAML, responses={
        "GetProjectSettings": {"getProjectSettings": {"projectId": "77"}},
        "CreateFolder": {"createFolder": {}}})
    res, err = call(ctx, "create_folder", projectKey="P", path="x/y/")
    assert not err and res["path"] == "/x/y"
    assert ctx.xray.calls_of("CreateFolder")[0] == {"projectId": "77", "path": "/x/y"}
    res, err = call(ctx, "create_folder", projectKey="P", path="/z", numericProjectId="77")
    assert not err and ctx.xray.calls_of("CreateFolder")[1]["projectId"] == "77"
    assert len(ctx.xray.calls_of("GetProjectSettings")) == 1


@pytest.mark.parametrize("tool,extra", [
    ("create_folder", {}),
    ("delete_folder", {"confirm": True}),
    ("rename_folder", {"newName": "b"}),
    ("move_folder", {"destinationPath": "/"}),
    ("add_tests_to_folder", {"testKeys": ["P-1"]}),
])
def test_foreign_numeric_project_id_refused_before_any_write(tool, extra):
    ctx = make_context(YAML, responses={
        "GetProjectSettings": {"getProjectSettings": {"projectId": "77"}},
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "9", "jira": {"key": "P-1"}}]}}})
    res, err = call(ctx, tool, projectKey="P", path="/a", numericProjectId="5", **extra)
    assert err and "does not belong to project P" in res["error"]
    assert {op for op, _ in ctx.xray.calls} <= {"GetProjectSettings", "GetTestIssueIds"}


def test_create_folder_existing_is_ok():
    def boom(_):
        raise ToolError("Folder already exists")
    ctx = _ctx(YAML, responses={"CreateFolder": boom})
    res, err = call(ctx, "create_folder", projectKey="P", path="/a", numericProjectId="1")
    assert not err and res["created"] is True


def test_add_tests_to_folder():
    ctx = _ctx(YAML, responses={
        "GetTestIssueIds": {"getTests": {"results": [{"issueId": "9", "jira": {"key": "P-1"}}]}},
        "CreateFolder": {"createFolder": {}},
        "AddTestsToFolder": {"addTestsToFolder": {"warnings": ["w"]}}})
    res, err = call(ctx, "add_tests_to_folder", projectKey="P", path="a", testKeys=["P-1"],
                    numericProjectId="1")
    assert not err and res == {"path": "/a", "added": ["P-1"], "warnings": ["w"]}
    assert ctx.xray.calls_of("AddTestsToFolder")[0]["testIssueIds"] == ["9"]


def test_add_tests_rejects_foreign_key_without_calls():
    ctx = _ctx(YAML)
    res, err = call(ctx, "add_tests_to_folder", projectKey="P", path="a", testKeys=["Q-1"],
                    numericProjectId="1")
    assert err and ctx.xray.calls == []


def test_get_tests_in_folder():
    ctx = _ctx(YAML, responses={"GetTestsInFolder": {"getTests": {"total": 1, "results": [
        {"issueId": "9", "jira": {"key": "P-1", "summary": "s"}}]}}})
    res, _ = call(ctx, "get_tests_in_folder", projectKey="P", path="/a/", numericProjectId="1",
                  limit=10, start=5)
    assert res["tests"] == [{"key": "P-1", "summary": "s"}] and res["total"] == 1
    assert ctx.xray.calls_of("GetTestsInFolder")[0] == {
        "projectId": "1", "path": "/a", "limit": 10, "start": 5}


def test_list_folders_tree_cached_and_invalidated_by_write():
    pages = {"/": folder("/", 0, [{"name": "a", "path": "/a", "testsCount": 2}]),
             "/a": folder("/a", 2)}
    ctx = _ctx(YAML, responses={
        "GetFolder": lambda v: pages[v["path"]], "CreateFolder": {"createFolder": {}}})
    res, _ = call(ctx, "list_folders", projectKey="P", numericProjectId="1")
    assert res["complete"] and res["tree"]["children"][0]["path"] == "/a"
    assert len(ctx.xray.calls_of("GetFolder")) == 2
    call(ctx, "list_folders", projectKey="P", numericProjectId="1")
    assert len(ctx.xray.calls_of("GetFolder")) == 2  # cached
    call(ctx, "create_folder", projectKey="P", path="/b", numericProjectId="1")
    call(ctx, "list_folders", projectKey="P", numericProjectId="1")
    assert len(ctx.xray.calls_of("GetFolder")) == 4  # invalidated by the write


def test_list_folders_partial_on_error():
    def get(v):
        if v["path"] == "/a":
            raise ToolError("rate limited")
        return folder("/", 0, [{"name": "a", "path": "/a", "testsCount": 1}])
    ctx = _ctx(YAML, responses={"GetFolder": get})
    res, err = call(ctx, "list_folders", projectKey="P", numericProjectId="1")
    assert not err and res["complete"] is False and res["pendingPaths"] == ["/a"]


def test_rename_and_move_validate_and_call():
    ctx = _ctx(YAML, responses={"RenameFolder": {"renameFolder": {}},
                                        "MoveFolder": {"moveFolder": {}}})
    res, _ = call(ctx, "rename_folder", projectKey="P", path="/a", newName="b",
                  numericProjectId="1")
    assert res["renamed"] and ctx.xray.calls_of("RenameFolder")[0]["newName"] == "b"
    _, err = call(ctx, "rename_folder", projectKey="P", path="/a", newName="b/c",
                  numericProjectId="1")
    assert err
    res, _ = call(ctx, "move_folder", projectKey="P", path="/a", destinationPath="/",
                  numericProjectId="1")
    assert res["moved"]
    _, err = call(ctx, "move_folder", projectKey="P", path="/a", destinationPath="/a/b",
                  numericProjectId="1")
    assert err and len(ctx.xray.calls_of("MoveFolder")) == 1


def test_delete_folder_preview_then_confirm():
    ctx = _ctx(YAML, responses={"GetFolder": folder("/a", 4),
                                        "DeleteFolder": {"deleteFolder": "ok"}})
    res, err = call(ctx, "delete_folder", projectKey="P", path="/a/", numericProjectId="1")
    assert not err and res["wouldDelete"] == {"path": "/a", "testCount": 4, "subfolders": 0}
    assert ctx.xray.calls_of("DeleteFolder") == []
    res, _ = call(ctx, "delete_folder", projectKey="P", path="/a", confirm=True,
                  numericProjectId="1")
    assert res["deleted"] and len(ctx.xray.calls_of("DeleteFolder")) == 1


def test_read_only_blocks_writes_and_schema_strict():
    ctx = _ctx(YAML, read_only=True)
    for name in ("create_folder", "delete_folder", "rename_folder", "move_folder",
                 "add_tests_to_folder"):
        _, err = call(ctx, name, projectKey="P", path="/a", newName="x", destinationPath="/",
                      testKeys=["P-1"])
        assert err


def test_delete_preview_bypasses_cache_and_filter_is_not_recursive():
    counts = iter([4, 9])
    ctx = _ctx(YAML, responses={
        "GetFolder": lambda v: folder("/a", next(counts)),
        "GetTestsInFolder": {"getTests": {"total": 0, "results": []}}})
    ctx.folder_cache.set(("1", "/a"), folder("/a", 1)["getFolder"])
    r1, _ = call(ctx, "delete_folder", projectKey="P", path="/a", numericProjectId="1")
    r2, _ = call(ctx, "delete_folder", projectKey="P", path="/a", numericProjectId="1")
    assert r1["wouldDelete"]["testCount"] == 4 and r2["wouldDelete"]["testCount"] == 9
    call(ctx, "get_tests_in_folder", projectKey="P", path="/a", numericProjectId="1")
    from xray_cloud.tools.folders import _TESTS_IN_FOLDER
    assert "includeDescendants: false" in _TESTS_IN_FOLDER
