"""analyze_feature_tags: summarise the tags of .feature files (read-only, spec 6.4a)."""
from __future__ import annotations

import os
from typing import Any, Dict, List

from ..context import require_project
from ..errors import ToolError
from ..gherkin.parse import ParsedFeature, parse_feature
from ..gherkin.plan import existing_set_names
from ..gherkin.tagsummary import summarize_tags, tag_values
from ..localfiles import resolve_local_path
from ..registry import REGISTRY
from .features import _rules, read_feature
from .tests import _schema

MAX_FILES = 500


def _folder_files(env, folder: str, room: int) -> Dict[str, Any]:
    root = resolve_local_path(env, folder)
    if not os.path.isdir(root):
        raise ToolError("folder is not a directory: %s" % folder, path=root)
    found: List[str] = []
    capped = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".feature"):
                if len(found) >= room:
                    capped = True
                    break
                found.append(os.path.join(dirpath, name))
        if capped:
            break
    return {"paths": found, "capped": capped}


@REGISTRY.tool(
    "analyze_feature_tags",
    "Read-only. Summarise the tags used in .feature files (featureFilePaths and/or a folder, "
    "searched recursively for *.feature, at most 500 files; `capped` is reported when more "
    "exist) without writing anything. tagSummary: keyValue {key: {values {value: count}, "
    "separators, commaLists, where {feature, rule, scenario}, scenarios}}, plain {tag: {count, "
    "where}}, issueKeys (never subject to rules), handledByRules, unmatched (no rule matches) "
    "and valuesMatchingTestSets {key: [values that are the name of an existing Test Set in "
    "the project]}. Files that fail to parse are listed in errors {path, error}; the rest "
    "are still summarised. Tag rules are the project's settings/remembered rules. When the "
    "project has no rules or there are unmatched tags, propose rules from this summary: "
    "explain each proposed rule in plain words with its effect on a few concrete scenarios "
    "from the files, and apply nothing until the user agrees; then store the agreed rules "
    "with set_preferences. A rule has `tag` (key or keys), optional `split`, and one or more "
    "of `label`, `testSet`, `field`, `skip`; for example (an example only, not a default; "
    "tag names are neutral) {tag: \"group\", split: \",\", testSet: \"{value}\"} would turn "
    "@group=alpha,beta into Test Sets alpha and beta.",
    _schema({"featureFilePaths": {"type": "array", "items": {"type": "string"}},
             "folder": {"type": "string"}}, []))
def analyze_feature_tags(ctx, args):
    pk = args["projectKey"]
    project = require_project(ctx, pk, write=False)
    paths: List[str] = list(args.get("featureFilePaths") or [])
    folder = args.get("folder")
    if not paths and not folder:
        raise ToolError("Give featureFilePaths and/or folder")
    capped = False
    if folder:
        extra = _folder_files(ctx.env, folder, max(MAX_FILES - len(paths), 0))
        paths += extra["paths"]
        capped = extra["capped"]
    if len(paths) > MAX_FILES:
        paths, capped = paths[:MAX_FILES], True
    rules, source = _rules(ctx, pk, project)
    features: List[ParsedFeature] = []
    files: List[str] = []
    errors: List[Dict[str, str]] = []
    for path in paths:
        try:
            text, rel = read_feature(ctx.env, path)
        except ToolError as e:
            errors.append({"path": path, "error": e.message})
            continue
        try:
            features.append(parse_feature(text, rel))
            files.append(rel)
        except ToolError as e:
            errors.append({"path": rel, "error": e.message})
    summary = summarize_tags(features, rules,
                             existing_set_names(ctx, pk, tag_values(features)))
    out: Dict[str, Any] = {"projectKey": pk, "files": files, "errors": errors,
                           "rules": {"count": len(rules), "source": source},
                           "tagSummary": summary}
    if capped:
        out.update(capped=True, maxFiles=MAX_FILES)
    return out
