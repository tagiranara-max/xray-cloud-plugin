"""set_preferences and get_status (spec 6.3a "Changing them" and 6.7)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import choices
from ..config import (REGIONS, ai_created_problem, feature_import_problem,
                      find_settings_file, settings_search_paths)
from ..context import require_project
from ..errors import ToolError
from ..gherkin.tagrules import parse_rules
from ..jira_client import normalize_site_url
from ..protocol import SERVER_INFO
from ..registry import REGISTRY
from .tests import _schema, check_ai_label

PLUGIN_JSON = Path(__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"

_FEATURE_IMPORT = ("createTestSet", "backgrounds", "tagsAsLabels", "unmatchedTags", "tagRules")
_AI_CREATED = ("aiLabel", "aiLabelScope")
_SET_PARAMS = {
    "createTestSet": {"type": "boolean"},
    "backgrounds": {"type": "string", "description": "preconditions | inline"},
    "tagsAsLabels": {"type": "boolean"},
    "unmatchedTags": {"type": "string", "description": "keep | skip"},
    "aiLabel": {"type": "string", "description":
                "The user's own AI-created label (no whitespace); \"\" = don't mark."},
    "aiLabelScope": {"type": "string", "description": "tests | all"},
    "tagRules": {"type": "array", "items": {"type": "object"}, "description":
                 "Raw tag rules (replaces the remembered list; [] = none). Rule fields: tag or "
                 "match, split, label, testSet, field, skip. Example only, not a default: "
                 "[{\"tag\": \"group\", \"testSet\": \"{value}\"}]."},
    "clear": {"type": "array", "items": {"type": "string"}, "description":
              "Choice names whose remembered value is removed (they become unanswered)."},
}


def _validate(project, args: Dict[str, Any]) -> Dict[str, Any]:
    """The values to store, validated like the settings file (nothing is written here)."""
    values: Dict[str, Any] = {}
    for name in _FEATURE_IMPORT + _AI_CREATED:
        if args.get(name) is None:
            continue
        value = args[name]
        if name == "aiLabel":
            check_ai_label(value)
        elif name == "aiLabelScope":
            problem = ai_created_problem("scope", value)
            if problem:
                raise ToolError("aiLabelScope %s" % problem, aiLabelScope=value)
        else:
            problem = feature_import_problem(name, value)
            if problem:
                raise ToolError("%s %s" % (name, problem), **{name: value})
        values[name] = value
    if "tagRules" in values:  # validated for use, stored raw
        parse_rules(values["tagRules"], set(project.fields.get("test") or {}), "tagRules")
    return values


@REGISTRY.tool(
    "set_preferences",
    "Change the remembered import choices of a project (createTestSet, backgrounds, "
    "tagsAsLabels, unmatchedTags, aiLabel, aiLabelScope, tagRules) or remove some with `clear`. "
    "An explicit change: it overwrites the remembered values (validated like the settings "
    "file). Writes only the plugin's own preferences file, never Xray, so read-only mode does "
    "not block it. Returns every choice's effective value and source (call, settings, "
    "remembered, default or unanswered) and `warnings` when a settings-file value overrides "
    "something just set (a settings value always wins). tagRules are raw rules; any rule shown in "
    "documentation is an example only, never a default.",
    _schema(_SET_PARAMS, []))
def set_preferences(ctx, args):
    pk = args["projectKey"]
    project = require_project(ctx, pk, write=False)
    clear = list(args.get("clear") or [])
    unknown = sorted(n for n in clear if n not in choices.CHOICES)
    if unknown:
        raise ToolError("Unknown choice name(s) in clear: %s" % ", ".join(unknown),
                        known=sorted(choices.CHOICES))
    values = _validate(project, args)
    both = sorted(set(clear) & set(values))
    if both:
        raise ToolError("Cannot set and clear the same choice: %s" % ", ".join(both))
    if not values and not clear:
        raise ToolError("Nothing to do: give at least one value to set or names to clear")
    region = choices.region_of(ctx)
    if clear:
        ctx.preferences.clear(region, pk, clear)
    if values:
        ctx.preferences.update(region, pk, values)
    effective = choices.effective(ctx, pk)
    warnings = ["%s was saved, but the settings file sets it to %r, which always wins; edit "
                "the settings file to change it" % (n, effective[n]["value"])
                for n in values if effective[n]["source"] == "settings"]
    return {"projectKey": pk, "region": region, "set": sorted(values), "cleared": sorted(clear),
            "effective": effective, "warnings": warnings,
            "preferencesFile": str(ctx.preferences.path)}


def _version() -> str:
    """plugin.json "version" when present, else the MCP server's own version."""
    try:
        version = json.loads(PLUGIN_JSON.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        version = None
    return version if isinstance(version, str) else SERVER_INFO["version"]


def _settings_status(ctx) -> Dict[str, Any]:
    """Load errors are reported here instead of raised (settings load lazily, so a broken
    file never stops the server from starting)."""
    out: Dict[str, Any] = {"path": None, "searched": [str(p) for p in settings_search_paths(ctx.env)],
                           "error": None}
    try:
        settings = ctx.settings_store.get()
    except ToolError as exc:
        out["error"] = exc.message
        out["path"] = exc.data.get("file")
        return out
    out["path"] = str(settings.path) if settings.path else None
    return out


def _jira_status(ctx, check: bool) -> Dict[str, Any]:
    creds = ctx.jira_creds
    out: Dict[str, Any] = {"configured": creds is not None,
                           "site": normalize_site_url(creds.site_url) if creds else None}
    if check and creds is not None:
        try:
            who = ctx.jira.myself()
            out["reachable"] = True
            out["user"] = who.get("displayName") or who.get("accountId")
        except Exception as exc:
            out["reachable"] = False
            out["message"] = (exc.message if isinstance(exc, ToolError) else
                              "%s" % type(exc).__name__)[:300]
    return out


def _project_status(ctx, pk: str) -> Dict[str, Any]:
    eff = choices.effective(ctx, pk)
    return {"featureImport": {n: eff[n] for n in _FEATURE_IMPORT},
            "aiCreated": {n: eff[n] for n in _AI_CREATED},
            "unanswered": [n for n, v in eff.items() if v["source"] == "unanswered"]}


@REGISTRY.tool(
    "get_status",
    "Show the plugin's state: version, region and base URL, whether Xray credentials are "
    "configured (never their values), the settings file (path, locations searched, load "
    "error), projects, read-only mode, Jira (configured, site; with check=true also "
    "reachable and user, using one Jira /myself call), the preferences file, and per project "
    "the effective import choices with their source (call, settings, remembered, default or "
    "unanswered; `unanswered` lists the ones still to ask). To change a choice, call "
    "set_preferences or edit the user's settings file (a settings-file value always wins). "
    "Needs no projectKey. Contains no secrets.",
    {"type": "object", "properties": {"check": {"type": "boolean", "description":
                                                "Also verify Jira access (one /myself call)."}},
     "required": [], "additionalProperties": False})
def get_status(ctx, args):
    region = choices.region_of(ctx)
    settings = _settings_status(ctx)
    projects: List[str] = []
    if settings["error"] is None:
        projects = sorted(ctx.settings_store.get().projects)
    out: Dict[str, Any] = {
        "version": _version(), "region": region, "baseUrl": REGIONS[region],
        "credentials": {"configured": ctx.has_credentials},
        "settings": settings, "projects": projects, "readOnly": bool(ctx.read_only),
        "jira": _jira_status(ctx, bool(args.get("check"))),
        "preferencesFile": str(ctx.preferences.path), "perProject": {}}
    try:
        out["perProject"] = {pk: _project_status(ctx, pk) for pk in projects}
    except ToolError as exc:  # unreadable preferences file: report, keep the rest
        out["preferencesError"] = exc.message
    return out
