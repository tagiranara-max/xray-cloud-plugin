from __future__ import annotations

import base64
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..context import (PartialFailure, ServerContext, check_issue_in_project,
                       require_project, with_permission_reason)
from ..errors import ToolError
from ..localfiles import PATH_NOTE, read_local_file
from ..registry import REGISTRY
from ..stepcache import CachedTest
from .tests import MAX_LIMIT, _schema

# -- GraphQL ---------------------------------------------------------------------

_GET_STEPS = """
query GetTestSteps($jql: String!) {
  getTests(jql: $jql, limit: 1) {
    results {
      issueId
      testType { name kind }
      gherkin
      folder { path }
      steps {
        id
        action
        data
        result
        attachments { id filename downloadLink }
        callTestIssueId
      }
      jira(fields: ["key"])
    }
  }
}
"""
_GET_CALLED = """
query GetCalledTests($issueIds: [String], $limit: Int!) {
  getTests(issueIds: $issueIds, limit: $limit) {
    results {
      issueId
      steps { id action data result attachments { id filename } }
      jira(fields: ["key", "summary"])
    }
  }
}
"""
_ADD_STEP = """
mutation AddTestStep($issueId: String!, $step: CreateStepInput!) {
  addTestStep(issueId: $issueId, step: $step) {
    id
    action
    data
    result
    attachments { id filename }
  }
}
"""
_UPDATE_STEP = """
mutation UpdateTestStep($stepId: String!, $step: UpdateStepInput!) {
  updateTestStep(stepId: $stepId, step: $step) { __typename }
}
"""
_REMOVE_STEP = """
mutation RemoveTestStep($stepId: String!) { removeTestStep(stepId: $stepId) }
"""

_MARKUP = re.compile(r"!xray-attachment://([^|!\s]+)([^!]*)!")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif")
_NON_STEP_KINDS = ("Gherkin", "Unstructured")


# -- Step model ------------------------------------------------------------------

@dataclass
class Upload:
    filename: str
    mime_type: str
    data: bytes
    old_id: Optional[str] = None  # id of the attachment this re-uploads (tail rebuilds)

    def to_input(self) -> dict:
        return {"filename": self.filename, "mimeType": self.mime_type,
                "data": base64.b64encode(self.data).decode("ascii")}


@dataclass
class Step:
    """A step to (re-)create. `original` is its 1-based number before the rebuild,
    None for a brand-new step."""
    action: str
    result: str
    data: Optional[str] = None
    attachments: List[Upload] = field(default_factory=list)
    original: Optional[int] = None

    def describe(self) -> dict:
        out: Dict[str, Any] = {"action": self.action, "data": self.data, "result": self.result,
                               "attachments": [u.filename for u in self.attachments]}
        if self.original is not None:
            out = dict({"originalStep": self.original}, **out)
        return out


def _mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def read_local_attachments(env, paths: Optional[List[str]]) -> List[Upload]:
    """Read every local file up front so a missing/unreadable path fails before any mutation."""
    out = []
    for p in paths or []:
        full, data = read_local_file(env, p, "Attachment file")
        name = os.path.basename(full)
        out.append(Upload(name, _mime(name), data))
    return out


def _non_blank(value: Optional[str], name: str) -> None:
    if value is not None and value.strip() == "":
        raise ToolError("'%s' must not be blank" % name)


# -- Fetching ----------------------------------------------------------------------

def load_test(ctx: ServerContext, project_key: str, test_key: str,
              use_cache: bool = False) -> CachedTest:
    """Return the test's steps; fresh from Xray unless use_cache (fresh results refresh
    the cache). Mutating tools always read fresh so step ids are never stale."""
    check_issue_in_project(test_key, project_key)
    if use_cache:
        hit = ctx.step_cache.get(test_key)
        if hit is not None:
            return hit
    data = ctx.xray.graphql(_GET_STEPS, {"jql": "issue = %s" % test_key})
    results = ((data.get("getTests") or {}).get("results")) or []
    found = (results[0].get("jira") or {}).get("key") if results else None
    if not results or (found is not None and found != test_key):
        raise ToolError("Test not found: %s" % test_key, testKey=test_key)
    r = results[0]
    tt = r.get("testType") or {}
    entry = CachedTest(key=test_key, issue_id=str(r["issueId"]), steps=list(r.get("steps") or []),
                       test_type=tt.get("name"), kind=tt.get("kind"), gherkin=r.get("gherkin"),
                       folder_path=(r.get("folder") or {}).get("path"), fetched_at=time.time())
    ctx.step_cache.set(test_key, entry)
    return entry


def compact_step(raw: dict, n: int) -> dict:
    if raw.get("callTestIssueId"):
        return {"n": n, "callTestIssueId": raw["callTestIssueId"]}
    return {"n": n, "action": raw.get("action"), "data": raw.get("data"),
            "result": raw.get("result"),
            "attachments": [a.get("filename") for a in raw.get("attachments") or []]}


def compact_steps(steps: List[dict]) -> List[dict]:
    return [compact_step(s, i + 1) for i, s in enumerate(steps)]


def _require_step_test(test: CachedTest) -> None:
    if test.kind in _NON_STEP_KINDS:
        raise ToolError("Test %s is a %s test (%s); steps apply only to step-based (Manual) tests"
                        % (test.key, test.kind, test.test_type or "unknown type"),
                        testKey=test.key)


def _check_range(value: int, n: int, name: str, test_key: str) -> None:
    if n == 0:
        raise ToolError("%s %d is out of range: test %s has no steps" % (name, value, test_key),
                        validRange=[])
    if not 1 <= value <= n:
        raise ToolError("%s %d is out of range: valid range is 1-%d (test %s has %d steps)"
                        % (name, value, n, test_key, n), validRange=[1, n])


def _final_state(ctx: ServerContext, project_key: str, test_key: str) -> dict:
    """Steps after a successful mutation; a failing read must not turn success into an error."""
    try:
        return {"steps": compact_steps(load_test(ctx, project_key, test_key).steps)}
    except Exception as e:
        return {"steps": None, "stepsError": "Mutation succeeded but re-reading steps failed: %s" % e}


# -- Tail rebuild (shared by insert and move) --------------------------------------

def prepare_tail(ctx: ServerContext, test: CachedTest, start_index: int,
                 allow_attachment_loss: bool = False) -> Tuple[List[Step], List[str]]:
    """Turn test.steps[start_index:] into Steps, refusing call-test steps and downloading
    every attachment. Attachments without a download link would be lost, so they are refused
    unless allow_attachment_loss. Performs no mutation; any failure here leaves Xray untouched."""
    tail = test.steps[start_index:]
    for i, raw in enumerate(tail):
        if raw.get("callTestIssueId"):
            raise ToolError(
                "Cannot rebuild steps of %s from position %d: step %d is a call-test step and "
                "cannot be recreated automatically. Append at the end instead, or reorder the "
                "steps manually in Xray." % (test.key, start_index + 1, start_index + i + 1),
                stepNumber=start_index + i + 1)
    unlinked = [(start_index + i + 1, att.get("filename"))
                for i, raw in enumerate(tail) for att in raw.get("attachments") or []
                if not att.get("downloadLink")]
    if unlinked and not allow_attachment_loss:
        raise ToolError(
            "Cannot rebuild steps of %s: %s cannot be downloaded (no download link) and would be "
            "lost. Pass allowAttachmentLoss: true to proceed without them."
            % (test.key, ", ".join("attachment '%s' on step %d" % (f, n) for n, f in unlinked)),
            attachmentsWithoutLink=[{"stepNumber": n, "filename": f} for n, f in unlinked])
    warnings: List[str] = []
    steps: List[Step] = []
    for i, raw in enumerate(tail):
        uploads = []
        for att in raw.get("attachments") or []:
            link = att.get("downloadLink")
            if not link:
                warnings.append("Attachment '%s' on step %d has no download link and was not "
                                "carried over (allowAttachmentLoss)"
                                % (att.get("filename"), start_index + i + 1))
                continue
            data = ctx.xray.download(link)
            uploads.append(Upload(att["filename"], _mime(att["filename"]), data, att.get("id")))
        steps.append(Step(raw.get("action") or "", raw.get("result") or "", raw.get("data"),
                          uploads, start_index + i + 1))
    return steps, warnings


def _add_step(ctx: ServerContext, issue_id: str, step: Step) -> dict:
    step_input: Dict[str, Any] = {"action": step.action, "result": step.result, "data": step.data}
    if step.attachments:
        step_input["attachments"] = [u.to_input() for u in step.attachments]
    data = ctx.xray.graphql(_ADD_STEP, {"issueId": issue_id, "step": step_input})
    return data.get("addTestStep") or {}


def _rewrite_markup(ctx: ServerContext, step: Step, created: dict, warnings: List[str]) -> None:
    """Re-point inline !xray-attachment://oldId! markup at the re-uploaded attachments."""
    old = [u for u in step.attachments if u.old_id]
    if not old:
        return
    available = list(created.get("attachments") or [])
    id_map: Dict[str, Optional[str]] = {}
    for u in old:
        match = next((a for a in available if a.get("filename") == u.filename), None)
        if match is not None:
            available.remove(match)
            id_map[u.old_id] = match.get("id")
        else:
            id_map[u.old_id] = None
            warnings.append("Attachment '%s' could not be matched after re-upload; its inline "
                            "markup was removed" % u.filename)

    def sub(m: "re.Match") -> str:
        if m.group(1) not in id_map:
            return m.group(0)
        new_id = id_map[m.group(1)]
        return "" if new_id is None else "!xray-attachment://%s%s!" % (new_id, m.group(2))

    update = {}
    for name in ("action", "data", "result"):
        value = getattr(step, name)
        if value:
            new_value = _MARKUP.sub(sub, value)
            if new_value != value:
                update[name] = new_value
    if update:
        ctx.xray.graphql(_UPDATE_STEP, {"stepId": created.get("id"), "step": update})


def _fresh_steps(ctx: ServerContext, test: CachedTest) -> Tuple[Optional[List[dict]], Optional[str]]:
    try:
        project_key = test.key.rsplit("-", 1)[0]
        return load_test(ctx, project_key, test.key).steps, None
    except Exception as e:
        return None, str(e)


def _current_steps_report(ctx: ServerContext, test: CachedTest) -> dict:
    steps, error = _fresh_steps(ctx, test)
    if steps is None:
        return {"currentSteps": None, "currentStepsError": error}
    return {"currentSteps": compact_steps(steps)}


def rebuild_tail(ctx: ServerContext, test: CachedTest, start_index: int,
                 new_tail: List[Step], warnings: Optional[List[str]] = None) -> List[str]:
    """Delete test.steps[start_index:], then create new_tail in order (Xray only appends).
    Callers must have downloaded attachments into new_tail already (prepare_tail).
    Returns warnings (seeded with `warnings`, e.g. from prepare_tail). If a mutation fails and the test changed, raises PartialFailure with
    the fresh current steps and the steps that were not (re-)created."""
    tail = test.steps[start_index:]
    for i, raw in enumerate(tail):
        if raw.get("callTestIssueId"):
            raise ToolError("Step %d of %s is a call-test step and cannot be recreated"
                            % (start_index + i + 1, test.key))
    warnings = list(warnings or [])
    removed: Set[str] = set()
    in_flight: Optional[str] = None
    added = 0
    stage = "starting"
    ctx.step_cache.invalidate(test.key)  # from here on Xray may be in a partial state
    try:
        for i, raw in enumerate(tail):
            stage = "removing original step %d" % (start_index + i + 1)
            in_flight = raw["id"]
            ctx.xray.graphql(_REMOVE_STEP, {"stepId": raw["id"]})
            removed.add(raw["id"])
        in_flight = None
        for i, step in enumerate(new_tail):
            stage = "creating step %d" % (start_index + i + 1)
            created = _add_step(ctx, test.issue_id, step)
            added += 1
            stage = "rewriting inline attachment markup of step %d" % (start_index + i + 1)
            _rewrite_markup(ctx, step, created, warnings)
    except Exception as e:
        raise _rebuild_failure(ctx, test, new_tail, removed, in_flight, added, stage, e,
                               warnings) from None
    return warnings


def _rebuild_failure(ctx: ServerContext, test: CachedTest, new_tail: List[Step],
                     removed: Set[str], in_flight: Optional[str], added: int, stage: str,
                     cause: Exception, warnings: Optional[List[str]] = None) -> Exception:
    """Build the error for a failed rebuild. Returns `cause` unchanged when a fresh read shows
    the test untouched. Otherwise the fresh state decides which original steps are gone; if
    it cannot be read, steps whose removal was confirmed or in flight count as gone."""
    current, current_error = _fresh_steps(ctx, test)
    original_ids = [s.get("id") for s in test.steps]
    if current is not None and added == 0 and [s.get("id") for s in current] == original_ids:
        return cause
    current_ids = None if current is None else {s.get("id") for s in current}

    def lost(step_id: str) -> bool:
        if current_ids is not None:
            return step_id not in current_ids
        return step_id in removed or step_id == in_flight

    pending = new_tail[added:]
    lost_steps = [s for s in pending
                  if s.original is not None and lost(test.steps[s.original - 1]["id"])]
    recovery_dir, not_readded = _save_lost_attachments(lost_steps)
    not_added = [s.describe() for s in pending if s.original is None]
    message = ("Rebuilding the steps of %s failed while %s; the test is now only partially "
               "updated. currentSteps is what exists in Xray now (the operation that failed may "
               "still have taken effect - check it); notReAdded lists original steps that were "
               "deleted and not re-created; notAdded lists new steps that were not created."
               % (test.key, stage))
    if recovery_dir:
        message += (" Attachments of the deleted steps were saved under %s; each savedTo path "
                    "can be passed back to add_test_steps as an attachment." % recovery_dir)
    data: Dict[str, Any] = {"testKey": test.key, "cause": str(cause)}
    if current is None:
        data.update({"currentSteps": None, "currentStepsError": current_error})
    else:
        data["currentSteps"] = compact_steps(current)
    data["notReAdded"] = not_readded
    data["notAdded"] = not_added
    if recovery_dir:
        data["recoveryDir"] = recovery_dir
    if warnings:
        data["warnings"] = list(warnings)
    return with_permission_reason(PartialFailure(message, **data), cause)


def _recovery_name(filename: str, k: int, used: Set[str]) -> str:
    """A safe, unique file name inside one step folder: path parts are stripped, '.'/'..'/''
    fall back to attachment-<k>, and a clash gets a -2, -3, ... suffix before the extension."""
    name = os.path.basename((filename or "").replace("\\", "/"))
    if name in ("", ".", ".."):
        name = "attachment-%d" % k
    stem, ext = os.path.splitext(name)
    candidate, i = name, 1
    while candidate.lower() in used:
        i += 1
        candidate = "%s-%d%s" % (stem, i, ext)
    used.add(candidate.lower())
    return candidate


def _save_lost_attachments(lost_steps: List[Step]) -> Tuple[Optional[str], List[dict]]:
    """Removing a step deleted its attachments in Xray; the downloaded bytes are the only copy
    left, so write them to a recovery directory and report {filename, savedTo} per attachment.
    Never raises: a failure is recorded as saveError so the original PartialFailure survives."""
    described = [s.describe() for s in lost_steps]
    if not any(s.attachments for s in lost_steps):
        return None, described
    try:
        root = tempfile.mkdtemp(prefix="xray-steps-recovery-")
    except Exception as e:
        for d, s in zip(described, lost_steps):
            d["attachments"] = [{"filename": u.filename, "savedTo": None,
                                 "saveError": str(e)} for u in s.attachments]
        return None, described
    for d, s in zip(described, lost_steps):
        out = []
        used: Set[str] = set()
        for k, u in enumerate(s.attachments, 1):
            entry: Dict[str, Any] = {"filename": u.filename}
            try:
                folder = os.path.join(root, "step-%d" % s.original)
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, _recovery_name(u.filename, k, used))
                with open(path, "xb") as fh:  # exclusive: never overwrite another attachment
                    fh.write(u.data)
                entry["savedTo"] = path
            except Exception as e:
                entry["savedTo"] = None
                entry["saveError"] = str(e)
            out.append(entry)
        d["attachments"] = out
    return root, described


# -- Schemas -----------------------------------------------------------------------

_TEST_KEY = {"type": "string", "description": "Test issue key, e.g. PROJ-12"}
_STEP_NO = {"type": "integer", "minimum": 1}
_PATHS = {"type": "array", "items": {"type": "string"},
          "description": "Local file paths to attach. " + PATH_NOTE}
_STEP_IN = {"type": "object", "properties": {
    "action": {"type": "string"},
    "result": {"type": "string", "description": "Expected result (optional; Xray allows empty)"},
    "data": {"type": "string", "description": "Test data (optional)"},
    "attachments": _PATHS}, "required": ["action"], "additionalProperties": False,
    "description": "A step: action is required; result and data are optional"}
_ALLOW_LOSS = {"type": "boolean",
               "description": "Proceed even if attachments of re-created steps have no download "
                              "link and would be lost (default false: refuse)"}
_POSITION = {"type": "object", "properties": {
    "at": {"type": "string", "enum": ["start", "end", "after"]},
    "stepNumber": {"type": "integer", "minimum": 1}},
    "required": ["at"], "additionalProperties": False,
    "description": 'Where to insert: {"at":"end"} (default), {"at":"start"} or '
                   '{"at":"after","stepNumber":N} (1-based)'}


# -- Tools -------------------------------------------------------------------------

@REGISTRY.tool(
    "get_test_steps",
    "Get the steps of a test as [{n, action, data, result, attachments}] (1-based n). "
    "Call-test steps include the called test's key, summary and steps. Gherkin tests return "
    "the scenario text instead. Results are cached for a few minutes; fromCache/cachedAt show "
    "freshness and forceRefresh=true bypasses the cache.",
    _schema({"testKey": _TEST_KEY, "forceRefresh": {"type": "boolean"}}, ["testKey"]))
def get_test_steps(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    require_project(ctx, pk, write=False)
    check_issue_in_project(key, pk)
    force = args.get("forceRefresh", False)
    cached = None if force else ctx.step_cache.get(key)
    test = cached or load_test(ctx, pk, key)
    out: Dict[str, Any] = {"testKey": key}
    if test.folder_path is not None:
        out["folderPath"] = test.folder_path
    if test.kind == "Gherkin":
        out["gherkin"] = test.gherkin or ""
    else:
        out["steps"] = _with_called_steps(ctx, test.steps)
    out["fromCache"] = cached is not None
    if cached is not None:
        out["cachedAt"] = datetime.fromtimestamp(test.fetched_at, timezone.utc).isoformat()
    return out


def _with_called_steps(ctx: ServerContext, steps: List[dict]) -> List[dict]:
    out = compact_steps(steps)
    ids = sorted({s["callTestIssueId"] for s in steps if s.get("callTestIssueId")})
    if not ids:
        return out
    by_id: Dict[str, dict] = {}
    for start in range(0, len(ids), MAX_LIMIT):  # Xray returns at most 100 per call
        batch = ids[start:start + MAX_LIMIT]
        data = ctx.xray.graphql(_GET_CALLED, {"issueIds": batch, "limit": len(batch)})
        by_id.update((str(r.get("issueId")), r)
                     for r in ((data.get("getTests") or {}).get("results") or []))
    for item in out:
        called = by_id.get(str(item.get("callTestIssueId")))
        if called:
            jira = called.get("jira") or {}
            item["calledTest"] = jira.get("key")
            item["calledTestSummary"] = jira.get("summary")
            item["calledSteps"] = compact_steps(called.get("steps") or [])
    return out


@REGISTRY.tool(
    "add_test_steps",
    "Add steps to a Manual test at a position: end (default), start, or after step N. "
    "Inserting before existing steps deletes and re-creates the later steps in order, carrying "
    "over their attachments (downloaded before anything is changed). Refused when a later step "
    "is a call-test step. attachments are local file paths.",
    _schema({"testKey": _TEST_KEY, "steps": {"type": "array", "items": _STEP_IN, "minItems": 1},
             "position": _POSITION, "allowAttachmentLoss": _ALLOW_LOSS}, ["testKey", "steps"]),
    write=True)
def add_test_steps(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    require_project(ctx, pk, write=True)
    check_issue_in_project(key, pk)
    position = args.get("position") or {"at": "end"}
    if position["at"] == "after" and "stepNumber" not in position:
        raise ToolError("position.stepNumber is required when position.at is 'after'")
    for s in args["steps"]:
        _non_blank(s["action"], "action")
        _non_blank(s.get("result"), "result")
        _non_blank(s.get("data"), "data")
    new_steps = [Step(s["action"], s.get("result", ""), s.get("data"),
                      read_local_attachments(ctx.env, s.get("attachments"))) for s in args["steps"]]
    test = load_test(ctx, pk, key)
    _require_step_test(test)
    n = len(test.steps)
    warnings: List[str] = []
    if position["at"] == "end":
        start = n
        ctx.step_cache.invalidate(key)
        added = 0
        try:
            for step in new_steps:
                _add_step(ctx, test.issue_id, step)
                added += 1
        except Exception as e:
            raise _rebuild_failure(ctx, test, new_steps, set(), None, added,
                                   "creating new step %d" % (n + added + 1), e) from None
    else:
        if position["at"] == "start":
            start = 0
        else:
            _check_range(position["stepNumber"], n, "position.stepNumber", key)
            start = position["stepNumber"]
        tail, warnings = prepare_tail(ctx, test, start, args.get("allowAttachmentLoss", False))
        warnings = rebuild_tail(ctx, test, start, new_steps + tail, warnings)
    out: Dict[str, Any] = {"testKey": key,
                           "added": list(range(start + 1, start + len(new_steps) + 1))}
    out.update(_final_state(ctx, pk, key))
    if warnings:
        out["warnings"] = warnings
    return out


@REGISTRY.tool(
    "move_test_step",
    "Move a step of a Manual test from position fromStep to toStep (both 1-based); the other "
    "steps keep their relative order. Steps from the earlier of the two positions onward are "
    "deleted and re-created with their attachments. Refused when one of them is a call-test step.",
    _schema({"testKey": _TEST_KEY, "fromStep": _STEP_NO, "toStep": _STEP_NO,
             "allowAttachmentLoss": _ALLOW_LOSS},
            ["testKey", "fromStep", "toStep"]),
    write=True)
def move_test_step(ctx, args):
    pk, key = args["projectKey"], args["testKey"]
    src, dst = args["fromStep"], args["toStep"]
    require_project(ctx, pk, write=True)
    test = load_test(ctx, pk, key)
    _require_step_test(test)
    n = len(test.steps)
    _check_range(src, n, "fromStep", key)
    _check_range(dst, n, "toStep", key)
    if src == dst:
        return {"testKey": key, "moved": False, "steps": compact_steps(test.steps)}
    order = list(range(n))
    order.insert(dst - 1, order.pop(src - 1))
    start = min(src, dst) - 1
    tail, warnings = prepare_tail(ctx, test, start, args.get("allowAttachmentLoss", False))
    warnings = rebuild_tail(ctx, test, start, [tail[i - start] for i in order[start:]], warnings)
    out: Dict[str, Any] = {"testKey": key, "moved": {"from": src, "to": dst}}
    out.update(_final_state(ctx, pk, key))
    if warnings:
        out["warnings"] = warnings
    return out


@REGISTRY.tool(
    "edit_test_step",
    "Update a step of a Manual test (stepNumber is 1-based). Provide at least one of action, "
    "result, data, attachments (local file paths, added to the step). embedAttachmentsIn "
    "(action|data|result) also embeds the newly uploaded images inline in that field.",
    _schema({"testKey": _TEST_KEY, "stepNumber": _STEP_NO, "action": {"type": "string"},
             "result": {"type": "string"}, "data": {"type": "string"}, "attachments": _PATHS,
             "embedAttachmentsIn": {"type": "string", "enum": ["action", "data", "result"]}},
            ["testKey", "stepNumber"]),
    write=True)
def edit_test_step(ctx, args):
    pk, key, number = args["projectKey"], args["testKey"], args["stepNumber"]
    require_project(ctx, pk, write=True)
    check_issue_in_project(key, pk)
    text = {k: args[k] for k in ("action", "result", "data") if args.get(k) is not None}
    paths = args.get("attachments") or []
    if not text and not paths:
        raise ToolError("Provide at least one of: action, result, data, attachments")
    for k, v in text.items():
        _non_blank(v, k)
    embed = args.get("embedAttachmentsIn")
    if embed and not paths:
        raise ToolError("embedAttachmentsIn requires attachments")
    uploads = read_local_attachments(ctx.env, paths)
    test = load_test(ctx, pk, key)
    _require_step_test(test)
    _check_range(number, len(test.steps), "stepNumber", key)
    target = test.steps[number - 1]
    if target.get("callTestIssueId"):
        raise ToolError("Step %d of %s is a call-test step; edit the called test's steps instead"
                        % (number, key))
    update: Dict[str, Any] = dict(text)
    if uploads:
        update["attachments"] = {"add": [u.to_input() for u in uploads]}
    before = {a.get("id") for a in target.get("attachments") or []}
    ctx.step_cache.invalidate(key)
    ctx.xray.graphql(_UPDATE_STEP, {"stepId": target["id"], "step": update})
    if embed:
        try:
            _embed(ctx, pk, key, target["id"], before, embed)
        except Exception as e:
            data: Dict[str, Any] = {"testKey": key, "cause": str(e)}
            data.update(_current_steps_report(ctx, test))
            raise with_permission_reason(
                PartialFailure("Step %d of %s was updated and its attachments uploaded, but "
                               "embedding them in %s failed" % (number, key, embed), **data),
                e) from None
    out: Dict[str, Any] = {"testKey": key, "updatedStep": number}
    out.update(_final_state(ctx, pk, key))
    return out


def _embed(ctx: ServerContext, pk: str, key: str, step_id: str, before: Set[str],
           field_name: str) -> None:
    fresh = load_test(ctx, pk, key)
    step = next((s for s in fresh.steps if s.get("id") == step_id), None)
    if step is None:
        raise ToolError("Step %s disappeared from %s" % (step_id, key))
    images = [a for a in step.get("attachments") or []
              if a.get("id") not in before
              and os.path.splitext(a.get("filename") or "")[1].lower() in _IMAGE_EXT]
    if not images:
        return
    markup = "\n".join("!xray-attachment://%s|width=260,height=270!" % a["id"] for a in images)
    current = step.get(field_name) or ""
    ctx.step_cache.invalidate(key)
    ctx.xray.graphql(_UPDATE_STEP, {"stepId": step_id, "step": {
        field_name: current + "\n\n" + markup if current else markup}})


@REGISTRY.tool(
    "remove_test_step",
    "Delete a step of a Manual test by its 1-based step number. The result echoes the removed "
    "step's content so it can be re-added.",
    _schema({"testKey": _TEST_KEY, "stepNumber": _STEP_NO}, ["testKey", "stepNumber"]),
    write=True)
def remove_test_step(ctx, args):
    pk, key, number = args["projectKey"], args["testKey"], args["stepNumber"]
    require_project(ctx, pk, write=True)
    test = load_test(ctx, pk, key)
    _check_range(number, len(test.steps), "stepNumber", key)
    ctx.step_cache.invalidate(key)
    target = test.steps[number - 1]
    ctx.xray.graphql(_REMOVE_STEP, {"stepId": target["id"]})
    out: Dict[str, Any] = {"testKey": key, "removed": compact_step(target, number)}
    out.update(_final_state(ctx, pk, key))
    return out
