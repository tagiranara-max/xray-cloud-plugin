"""Native Cucumber feature import (Xray import/feature) and export (export/cucumber)."""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from typing import Any, Dict, List

from ..context import check_issue_in_project, require_project
from ..errors import ToolError
from ..fields import build_jira_fields
from .. import localfiles
from ..registry import REGISTRY
from .tests import _OBJ, _schema

MAX_EXPORT_BYTES = 100 * 1024 * 1024  # total uncompressed size of an export zip
_DRIVE = re.compile(r"^[A-Za-z]:")


@REGISTRY.tool(
    "import_feature_file",
    "Import a .feature file, or a .zip of feature files, with Xray's native importer: Xray "
    "creates or updates Cucumber tests and preconditions from the Gherkin. Fast and faithful for "
    "bulk loads, but it does no duplicate check, offers no choices, applies no tag rules, adds "
    "no AI label and has no preview; prefer create_tests_from_feature when you need any of "
    "those. fields/rawFields apply to the tests and preconditionFields to the preconditions "
    "(project settings defaults and required checks apply, before upload). folder is an optional "
    "Test Repository folder path. Returns the test and precondition keys and any errors.",
    _schema({"filePath": {"type": "string", "description": ".feature or .zip file (max 25 MB)"},
             "fields": _OBJ, "rawFields": _OBJ, "preconditionFields": _OBJ,
             "folder": {"type": "string"}}, ["filePath"]),
    write=True)
def import_feature_file(ctx, args):
    pk = args["projectKey"]
    project = require_project(ctx, pk, write=True)
    path = args["filePath"]
    if not isinstance(path, str) or not path.lower().endswith((".feature", ".zip")):
        raise ToolError("filePath must be a .feature or .zip file", filePath=path)
    test_fields = build_jira_fields(project, "test", args.get("fields"), args.get("rawFields"))
    pre_fields = build_jira_fields(project, "precondition", args.get("preconditionFields"), None)
    full, data = localfiles.read_local_file(ctx.env, path, "Feature file")
    ctype = "application/zip" if full.lower().endswith(".zip") else "text/plain"
    files: Dict[str, Any] = {"file": (os.path.basename(full), data, ctype)}
    if test_fields:
        files["testInfo"] = ("testInfo.json", json.dumps({"fields": test_fields}).encode("utf-8"),
                             "application/json")
    if pre_fields:
        files["precondInfo"] = ("precondInfo.json",
                                json.dumps({"fields": pre_fields}).encode("utf-8"),
                                "application/json")
    params = {"projectKey": pk}
    if args.get("folder"):
        params["folderPath"] = args["folder"]
    resp = ctx.xray.rest("POST", "import/feature", params=params,
                         multipart={"fields": {}, "files": files})
    resp = resp if isinstance(resp, dict) else {}

    def keys(name: str) -> List[str]:
        return [i.get("key") for i in (resp.get(name) or []) if isinstance(i, dict) and i.get("key")]

    return {"tests": keys("updatedOrCreatedTests"),
            "preconditions": keys("updatedOrCreatedPreconditions"),
            "errors": resp.get("errors") or []}


def _unsafe(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/") or _DRIVE.match(name):
        return True
    return any(seg == ".." for seg in name.split("/"))


def _plan(directory: str, body: bytes, overwrite: bool) -> Dict[str, bytes]:
    """-> {relative name: content}. Validates every entry and target; raises before any write."""
    if not body[:2] == b"PK":
        entries = {"export.feature": body}
    else:
        try:
            zf = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile:
            raise ToolError("Xray returned a corrupt zip archive") from None
        infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
        if sum(i.file_size for i in infos) > MAX_EXPORT_BYTES:
            raise ToolError("Export is too large: over %d MB uncompressed"
                            % (MAX_EXPORT_BYTES // (1024 * 1024)))
        entries = {}
        dupes: List[str] = []
        for i in infos:
            if i.filename in entries:
                dupes.append(i.filename)
            entries[i.filename] = zf.read(i)
        if dupes:
            raise ToolError("Export has duplicate entries: %s" % ", ".join(sorted(set(dupes))),
                            entries=sorted(set(dupes)))
    unsafe = [n for n in entries if _unsafe(n)]
    if unsafe:
        raise ToolError("Refusing to write unsafe entries (absolute, '..', backslash or drive "
                        "names): %s" % ", ".join(unsafe), entries=unsafe)
    root = os.path.realpath(directory)
    outside, exists, conflict = [], [], []
    names = set(entries)
    for n in entries:
        target = os.path.realpath(os.path.join(root, *n.split("/")))
        if os.path.commonpath([root, target]) != root or target == root:
            outside.append(n)
            continue
        parts = n.split("/")
        for k in range(1, len(parts)):
            anc = "/".join(parts[:k])
            if anc in names or (os.path.lexists(os.path.join(root, *parts[:k]))
                                and not os.path.isdir(os.path.join(root, *parts[:k]))):
                conflict.append(n)
                break
        else:
            if os.path.lexists(target) and (os.path.isdir(target) or not overwrite):
                exists.append(n)
    if outside:
        raise ToolError("Refusing to write outside the target directory: %s" % ", ".join(outside),
                        entries=outside)
    if conflict:
        raise ToolError("Entries conflict with a file where a directory is needed: %s"
                        % ", ".join(conflict), entries=conflict)
    if exists:
        raise ToolError("Refusing to overwrite existing files (pass overwrite: true to replace "
                        "files): %s" % ", ".join(exists), entries=exists)
    return entries


@REGISTRY.tool(
    "export_cucumber_features",
    "Export Cucumber tests as .feature files into a local directory (created if missing). Give "
    "exactly one of keys (test keys in the project) or filter (an Xray saved-filter id). Existing "
    "files are never overwritten unless overwrite is true; every file is checked before the first "
    "is written, so a refusal writes nothing. Returns the directory and the relative file paths.",
    _schema({"directory": {"type": "string"},
             "keys": {"type": "array", "items": {"type": "string"}},
             "filter": {"type": "string"},
             "overwrite": {"type": "boolean"}}, ["directory"]),
    write=False)
def export_cucumber_features(ctx, args):
    pk = args["projectKey"]
    require_project(ctx, pk, write=False)
    keys, flt = args.get("keys"), args.get("filter")
    if bool(keys) == bool(flt):
        raise ToolError("Provide exactly one of keys (non-empty) or filter")
    if keys:
        for k in keys:
            check_issue_in_project(k, pk)
        params = {"keys": ";".join(keys)}
    else:
        params = {"filter": str(flt)}
    directory = localfiles.resolve_local_path(ctx.env, args["directory"])
    if os.path.exists(directory) and not os.path.isdir(directory):
        raise ToolError("directory is not a directory: %s" % args["directory"], path=directory)
    body = ctx.xray.rest_bytes("GET", "export/cucumber", params=params)
    entries = _plan(directory, body, bool(args.get("overwrite")))
    written: List[str] = []
    try:
        for name, data in entries.items():
            target = os.path.join(directory, *name.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
            written.append(name)
    except OSError as e:
        raise ToolError("Export stopped while writing %s: %s" % (name, e.strerror or e),
                        written=written) from None
    return {"directory": directory, "files": sorted(written)}
