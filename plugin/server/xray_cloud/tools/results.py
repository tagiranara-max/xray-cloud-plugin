"""Import test execution results (JUnit, TestNG, NUnit, Robot, xray JSON, Cucumber, Behave)."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .. import resultmatch as rm
from ..context import ISSUE_KEY_RE, check_issue_in_project, dedupe, require_project
from ..errors import ToolError
from .. import requiredfields
from .. import localfiles
from ..registry import REGISTRY
from .tests import _OBJ, _REMEMBER, _schema

XML_FORMATS = ("junit", "testng", "nunit", "robot")
JSON_FORMATS = ("xray", "cucumber", "behave")
FORMATS = JSON_FORMATS + XML_FORMATS
PAGE = 100

_RUNS = """
query GetTestExecutionRuns($testExecIssueIds: [String], $limit: Int!, $start: Int!) {
  getTestRuns(testExecIssueIds: $testExecIssueIds, limit: $limit, start: $start) {
    total
    results { status { name } test { jira(fields: ["key"]) } }
  }
}
"""


def _endpoint(fmt: str, multipart: bool) -> str:
    base = "import/execution" if fmt == "xray" else "import/execution/" + fmt
    return base + "/multipart" if multipart else base


def _read_input(ctx, args: dict) -> bytes:
    has_file, has_content = args.get("filePath") is not None, args.get("content") is not None
    if has_file == has_content:
        raise ToolError("Provide exactly one of filePath or content",
                        filePath=has_file, content=has_content)
    if has_file:
        data = localfiles.read_local_file(ctx.env, args["filePath"], "Results file")[1]
        if not data:
            raise ToolError("Results file is empty: %s" % args["filePath"])
        return data
    data = args["content"].encode("utf-8")
    if not data:
        raise ToolError("Results content is empty")
    if len(data) > localfiles.MAX_FILE_BYTES:
        raise ToolError("Results content is too large: %.1f MB; the limit is %d MB"
                        % (len(data) / (1024 * 1024), localfiles.MAX_FILE_BYTES // (1024 * 1024)),
                        maxBytes=localfiles.MAX_FILE_BYTES)
    return data


def _run_summary(ctx, issue_id: str) -> Tuple[Dict[str, Any], List[str]]:
    """({total, byStatus}, test keys in run order) of a test execution."""
    by_status: Dict[str, int] = {}
    keys: List[str] = []
    total = start = 0
    while True:
        runs = (ctx.xray.graphql(_RUNS, {"testExecIssueIds": [issue_id], "limit": PAGE,
                                         "start": start}).get("getTestRuns")) or {}
        rows = runs.get("results") or []
        for r in rows:
            name = (r.get("status") or {}).get("name") or "UNKNOWN"
            by_status[name] = by_status.get(name, 0) + 1
            key = ((r.get("test") or {}).get("jira") or {}).get("key")
            if key:
                keys.append(key)
        total += len(rows)
        start += len(rows)
        if not rows or start >= (runs.get("total") or 0):
            return {"total": total, "byStatus": by_status}, keys


def _parse_json(data: bytes, fmt: str) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ToolError("The %s results are not valid UTF-8 JSON: %s" % (fmt, e)) from None


def _key_in_project(value: Any, pk: str, where: str) -> None:
    if not isinstance(value, str):
        raise ToolError("The report's %s must be an issue key" % where, value=value)
    check_issue_in_project(value, pk)


def check_xray_report(report: Any, pk: str) -> Optional[str]:
    """Refuse an Xray JSON report that points outside project `pk`: testExecutionKey,
    info.testPlanKey, info.project, tests[].testKey and tests[].testInfo.projectKey must all
    belong to it. Returns the report's testExecutionKey (None when absent)."""
    if not isinstance(report, dict):
        raise ToolError("Xray JSON results must be an object with a tests list")
    info = report.get("info")
    if info is not None and not isinstance(info, dict):
        raise ToolError("The report's info must be an object")
    tests = report.get("tests")
    if tests is not None and not isinstance(tests, list):
        raise ToolError("The report's tests must be a list")
    exec_key = report.get("testExecutionKey")
    if exec_key not in (None, ""):
        _key_in_project(exec_key, pk, "testExecutionKey")
    info = info or {}
    if info.get("testPlanKey") not in (None, ""):
        _key_in_project(info["testPlanKey"], pk, "info.testPlanKey")
    if info.get("project") not in (None, "", pk):
        raise ToolError("The report's info.project is %s, not %s" % (info["project"], pk),
                        project=info["project"], projectKey=pk)
    for t in tests or []:
        if not isinstance(t, dict):
            continue
        if t.get("testKey") not in (None, ""):
            _key_in_project(t["testKey"], pk, "testKey")
        ti = t.get("testInfo")
        if isinstance(ti, dict) and ti.get("projectKey") not in (None, "", pk):
            raise ToolError("The report's testInfo.projectKey is %s, not %s"
                            % (ti["projectKey"], pk), projectKey=pk)
    return exec_key or None


def _with_target(report: dict, exec_key: Optional[str], plan_key: Optional[str],
                 envs: List[str], project: Optional[str] = None) -> Optional[dict]:
    """The Xray JSON report aimed at an existing execution / plan / environments, with
    info.project set when missing (if `project` is given); None when nothing changes."""
    changed = dict(report)
    if exec_key:
        have = report.get("testExecutionKey")
        if have and have != exec_key:
            raise ToolError("The report names testExecutionKey %s, not %s" % (have, exec_key))
        changed["testExecutionKey"] = exec_key
    info = dict(report.get("info") or {})
    for name, value in (("testPlanKey", plan_key), ("testEnvironments", envs or None)):
        if value is None:
            continue
        if info.get(name) not in (None, "", [], value):
            raise ToolError("The report's info.%s differs from the %s argument"
                            % (name, name), report=info.get(name), argument=value)
        info[name] = value
    if project and not info.get("project"):
        info["project"] = project
    if info != (report.get("info") or {}):
        changed["info"] = info
    return changed if changed != report else None


def _check_envs(envs: Any) -> List[str]:
    if not isinstance(envs, list):
        raise ToolError("testEnvironments must be a list of environment names")
    for e in envs:
        if not isinstance(e, str) or not e.strip() or ";" in e:
            raise ToolError("Environment names must be non-empty strings without ';': %r" % (e,),
                            environment=e)
    return list(envs)


_FAILED = {"failed", "undefined", "ambiguous"}

Result = Tuple[str, str, str]  # (status, label, error message)


def _error_text(res: dict) -> str:
    err = res.get("error_message")
    if isinstance(err, list):  # Behave writes the message as a list of lines
        return "\n".join(str(e) for e in err)
    return str(err or "")


def _results(element: dict) -> List[Result]:
    """Every result of one Cucumber/Behave JSON element in run order: scenario hooks, and each
    step with its own before/after hooks (as cucumber-jvm emits them)."""
    out: List[Result] = []

    def add(item: Any, label: str) -> None:
        res = (item.get("result") or {}) if isinstance(item, dict) else {}
        out.append((str(res.get("status") or "").lower(), label, _error_text(res)))

    for hook in element.get("before") or []:
        add(hook, "Before hook")
    for st in element.get("steps") or []:
        if not isinstance(st, dict):
            continue
        name = _step_text(st) or "step"
        for hook in st.get("before") or []:
            add(hook, "Before step hook (%s)" % name)
        add(st, name)
        for hook in st.get("after") or []:
            add(hook, "After step hook (%s)" % name)
    for hook in element.get("after") or []:
        add(hook, "After hook")
    return out


def _step_text(st: dict) -> str:
    return ("%s %s" % (str(st.get("keyword") or "").strip(), st.get("name") or "")).strip()


def _status(results: List[Result], has_steps: bool) -> Tuple[str, Optional[str]]:
    """(Xray status, first failure): FAILED when a step or hook failed (or is undefined/
    ambiguous), PASSED when there are steps and everything passed, else TODO."""
    for status, label, error in results:
        if status in _FAILED:
            return "FAILED", ("%s: %s" % (label, error or status))[:2000]
    if has_steps and all(st == "passed" for st, _, _ in results):
        return "PASSED", None
    return "TODO", None


_RANK = {"FAILED": 2, "TODO": 1, "PASSED": 0}


def scenarios_to_xray(cases: List[rm.ScenarioCase], keys: List[Optional[str]], pk: str,
                      create: bool) -> dict:
    """Xray JSON for Cucumber/Behave scenarios: one test per resolved key (a Background's
    results count for the scenario after it; Scenario Outline rows of one key give one example
    status per row; other repeats keep the worst status; failures become the comment). With
    `create`, scenarios without a key become `testInfo` entries (a new Cucumber test named
    after the scenario, its steps as the definition); otherwise they are left out."""
    groups: Dict[Tuple[str, str], List[Tuple[str, Optional[str], bool]]] = {}
    first: Dict[Tuple[str, str], rm.ScenarioCase] = {}
    for c, key in zip(cases, keys):
        if key is None and not create:
            continue
        gk = ("key", key) if key else ("new", c.summary)
        results: List[Result] = []
        for bg in c.background:
            results += _results(bg)
        status, failure = _status(results + _results(c.element), bool(c.element.get("steps")))
        groups.setdefault(gk, []).append((status, failure, c.outline))
        first.setdefault(gk, c)
    tests: List[Dict[str, Any]] = []
    for gk, rows in groups.items():
        statuses = [st for st, _, _ in rows]
        if gk[0] == "key":
            item: Dict[str, Any] = {"testKey": gk[1]}
        else:
            steps = [_step_text(st) for st in first[gk].element.get("steps") or []
                     if isinstance(st, dict)]
            item = {"testInfo": {"projectKey": pk, "summary": gk[1], "type": "Cucumber",
                                 "scenario": "\n".join(s for s in steps if s)}}
        item["status"] = max(statuses, key=lambda st: _RANK[st])
        comments = [c for _, c, _ in rows if c]
        if comments:
            item["comment"] = "\n".join(comments)[:4000]
        if gk[0] == "key" and len(rows) > 1 and all(outline for _, _, outline in rows):
            item["examples"] = statuses
        tests.append(item)
    return {"tests": tests}


def _tag_scenarios(cases: List[rm.ScenarioCase], keys: List[Optional[str]], fmt: str) -> bool:
    """Give each resolved scenario exactly one Test-key tag (@TEST_<KEY>; Behave: TEST_<KEY>),
    which is the only thing Xray's Cucumber/Behave import maps by. True when a tag changed."""
    changed = False
    for c, key in zip(cases, keys):
        if key is None:
            continue
        tags = list(c.element.get("tags") or [])
        names = rm.tag_names(c.element, fmt)
        want = "@TEST_" + key
        keep = [t for t, n in zip(tags, names) if not rm.TEST_TAG.match(n) or n == want]
        if not any(n == want for n in names):
            keep.append(want[1:] if fmt == "behave" else {"name": want})
        if keep != tags:
            c.element["tags"] = keep
            changed = True
    return changed


# ---- matching: which test each result goes to ---------------------------------------------

CAP = 50  # list lengths in a needs_answer result
CANDIDATES = 10  # candidate keys shown per ambiguous result


@dataclass
class Resolution:
    keys: List[Optional[str]]             # per case: the test it goes to (None: unresolved)
    ambiguous: Dict[str, List[str]]       # name -> candidate keys
    missing: List[str]                    # names that match no test
    matched: int                          # names resolved (plus pre-keyed Xray JSON entries)
    notes: List[str] = field(default_factory=list)
    # Robot: per case, the matched test's current summary (None: not matched)
    current: List[Optional[str]] = field(default_factory=list)


def _resolve(names: List[str], pre: List[List[str]], terms: List[Optional[str]],
             lookup, explicit: Dict[str, str], extra_matched: int = 0) -> Resolution:
    """Per case: an explicit testKeys entry, else the keys the report names (pre), else the
    lookup of its term (summary or definition; None = nothing to look up). One key resolves
    it; several make it ambiguous; none, missing."""
    unknown = sorted(set(explicit) - set(names))
    if unknown:
        raise ToolError("testKeys names results that are not in the report: %s"
                        % ", ".join(unknown[:20]), unknown=unknown[:CAP])
    need = dedupe([t for n, p, t in zip(names, pre, terms)
                   if n not in explicit and not p and t is not None])
    found = lookup(need) if need else {}
    keys: List[Optional[str]] = []
    ambiguous: Dict[str, List[str]] = {}
    missing: List[str] = []
    for n, p, t in zip(names, pre, terms):
        if n in explicit:
            keys.append(explicit[n])
            continue
        cands = p or (found.get(t, []) if t is not None else [])
        if len(cands) == 1:
            keys.append(cands[0])
            continue
        keys.append(None)
        if cands:
            ambiguous.setdefault(n, list(cands))
        elif n not in missing:
            missing.append(n)
    missing = [n for n in missing if n not in ambiguous]
    matched = len(set(names) - set(ambiguous) - set(missing)) + extra_matched
    return Resolution(keys, ambiguous, missing, matched)


def _check_test_keys(value: Any, pk: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolError("testKeys must be an object {result name: test key}")
    for name, key in value.items():
        if not isinstance(key, str):
            raise ToolError("testKeys values must be issue keys", name=name, value=key)
        check_issue_in_project(key, pk)
    return {str(n).strip(): k for n, k in value.items()}


def _needs_answer(res: Resolution, fmt: str) -> dict:
    questions = []
    if res.ambiguous:
        questions.append({"id": "testKeys", "question": (
            "%d result(s) match more than one existing test. Which test should each go to? "
            "Answer with testKeys {name: KEY}." % len(res.ambiguous)),
            "context": {"ambiguous": len(res.ambiguous)}})
    if res.missing:
        what = ("Xray creates a Generic test for each (summary = the case name)"
                if fmt in XML_FORMATS else "a new Cucumber test is created for each"
                if fmt in ("cucumber", "behave") else "Xray creates a test for each")
        questions.append({"id": "allowCreateTests", "question": (
            "%d result(s) match no existing test. Create new Test issues for them (%s; they get "
            "no AI label)? Or map them to existing tests with testKeys {name: KEY}, or leave "
            "them out with skipMissing." % (len(res.missing), what)),
            "options": [True, False], "context": {"missing": len(res.missing)}})
    amb = []
    for n, c in res.ambiguous.items():
        item: Dict[str, Any] = {"name": n, "candidates": c[:CANDIDATES]}
        if len(c) > CANDIDATES:
            item["candidatesCount"] = len(c)
        amb.append(item)
    out: Dict[str, Any] = {"status": "needs_answer", "questions": questions,
                           "matched": res.matched, "ambiguous": amb[:CAP],
                           "ambiguousCount": len(amb), "missing": res.missing[:CAP],
                           "missingCount": len(res.missing)}
    if res.notes:
        out["notes"] = res.notes
    out["nothingImported"] = True
    return out


def _resolve_scenarios(ctx, pk: str, cases: List[rm.ScenarioCase],
                       explicit: Dict[str, str]) -> Resolution:
    return _resolve([c.name for c in cases], [c.tag_keys for c in cases],
                    [c.summary for c in cases], lambda ts: rm.find_by_summary(ctx, pk, ts),
                    explicit)


def _resolve_xml(ctx, pk: str, fmt: str, cases: List[rm.XmlCase],
                 explicit: Dict[str, str], known: Dict[str, str]) -> Resolution:
    summaries: Dict[str, str] = dict(known)  # key -> summary of Tests already looked up
    if fmt == "robot":
        # A key-shaped Robot tag maps the test only when that issue is a Test; other tags
        # (requirement links, dates such as ISO-8601) are left alone. A Test of another
        # project is refused. report_keys keeps only the Tests, so key injection replaces
        # just those tags.
        tagged = [k for c in cases for k in c.report_keys]
        tests = rm.existing_tests(ctx, tagged) if tagged else {}
        for k in tests:
            check_issue_in_project(k, pk)
        summaries.update(tests)
        for c in cases:
            c.report_keys = [k for k in c.report_keys if k in tests]
    pre = [c.report_keys for c in cases]
    notes: List[str] = []

    def lookup(defs):
        found, complete = rm.find_by_definition(ctx, pk, defs)
        if not complete:
            notes.append("The project has more than %d Generic tests; only those were checked "
                         "for matching definitions, so some 'missing' results may already "
                         "have a test. If creation is allowed, Xray then reuses that test and "
                         "may overwrite its summary or labels from the file (for Robot: rename "
                         "it to the Robot test's name)." % (rm.MAX_DEFINITION_PAGES * rm.PAGE))
        return found

    names = [c.name for c in cases]
    res = _resolve(names, pre, names, lookup, explicit)
    res.notes = notes
    if fmt == "robot":
        res.current = _robot_summaries(ctx, res, summaries)
    return res


def _robot_summaries(ctx, res: Resolution, summaries: Dict[str, str]) -> List[Optional[str]]:
    """The current summary of the test each Robot result is matched to. Xray renames a matched
    test to the Robot test's name (verified live), so the uploaded copy uses this summary."""
    unknown = [k for k in res.keys if k and k not in summaries]
    if unknown:
        summaries = dict(summaries, **rm.existing_tests(ctx, unknown))
    unreadable = sorted({k for k in res.keys if k and k not in summaries})
    if unreadable:
        raise ToolError("Cannot read the summary of %s, so Xray could rename it to the Robot "
                        "test's name; nothing was imported" % ", ".join(unreadable),
                        keys=unreadable)
    return [summaries.get(k) if k else None for k in res.keys]


def _xray_cases(report: dict) -> List[Tuple[int, str, Optional[str]]]:
    """(index, name, definition to look up) of each Xray JSON entry that would make Xray create
    or pick a test itself: no testKey, a testInfo. Generic entries are looked up by definition
    (Xray reuses the Generic test with the same definition)."""
    out = []
    for i, t in enumerate(report.get("tests") or []):
        if not isinstance(t, dict) or t.get("testKey") or not isinstance(t.get("testInfo"), dict):
            continue
        ti = t["testInfo"]
        name = str(ti.get("summary") or ti.get("definition") or "test %d" % (i + 1)).strip()
        generic = str(ti.get("type") or "").lower() == "generic" and \
            isinstance(ti.get("definition"), str) and ti["definition"]
        out.append((i, name, ti["definition"] if generic else None))
    # Entries with the same summary are named apart (summary + definition, else the entry
    # number), so testKeys and skipMissing act per entry.
    counts: Dict[str, int] = {}
    for _, n, _ in out:
        counts[n] = counts.get(n, 0) + 1
    named = []
    for i, n, d in out:
        if counts[n] > 1:
            ti = report["tests"][i]["testInfo"]
            detail = ti.get("definition") or ti.get("scenario")
            n = "%s (%s)" % (n, str(detail).strip().splitlines()[0] if detail else
                             "entry %d" % (i + 1))
        named.append((i, n, d))
    seen: Dict[str, int] = {}
    unique = []
    for i, n, d in named:
        seen[n] = seen.get(n, 0) + 1
        unique.append((i, n if seen[n] == 1 else "%s #%d" % (n, i + 1), d))
    return unique


def _inject_xml_key(el, fmt: str, key: str, old: List[str]) -> None:
    """Make the file name `key` as the case's test, the way Xray reads it."""
    if fmt == "robot":
        box = el.find("tags")
        if box is None:
            box = ET.Element("tags")
            status = el.find("status")
            el.insert(list(el).index(status) if status is not None else len(el), box)
        for t in list(box):
            if (t.text or "").strip() in old:
                box.remove(t)
        ET.SubElement(box, "tag").text = key
        return
    container, item, name, as_attr = {
        "junit": ("properties", "property", "test_key", True),
        "testng": ("attributes", "attribute", "test", False),
        "nunit": ("properties", "property", "Test", True)}[fmt]
    box = el.find(container)
    if box is None:
        box = ET.Element(container)
        el.insert(0, box)
    for p in list(box):
        if p.tag == item and p.get("name") == name:
            box.remove(p)
    p = ET.SubElement(box, item, {"name": name})
    if as_attr:
        p.set("value", key)
    else:
        p.text = key


# Entries that make Xray overwrite an existing test's fields. JUnit test_summary and tags
# verified live (summary and labels of a test matched by key or by definition were
# overwritten); test_description and the TestNG/NUnit names are removed as a precaution.
_UPDATING = {"junit": ("properties", "property", {"test_summary", "test_description", "tags"}),
             "testng": ("attributes", "attribute", {"test_summary", "test_description",
                                                    "summary", "description", "labels", "tags"}),
             "nunit": ("properties", "property", {"test_summary", "test_description",
                                                  "Description", "labels", "Labels", "tags"})}


def _strip_updates(el, fmt: str) -> List[str]:
    if fmt == "robot":
        # Xray adds a matched test's plain tags to its labels (verified live); key-shaped
        # tags (test keys, requirement links) stay.
        removed = []
        for box in el.findall("tags"):
            for t in list(box):
                text = (t.text or "").strip()
                if not ISSUE_KEY_RE.fullmatch(text):
                    box.remove(t)
                    removed.append(text)
        return removed
    if fmt not in _UPDATING:
        return []
    container, item, names = _UPDATING[fmt]
    removed = []
    for box in el.findall(container):
        for p in list(box):
            if p.tag == item and p.get("name") in names:
                box.remove(p)
                removed.append(p.get("name"))
    return removed


def _apply_xml(root, fmt: str, cases: List[rm.XmlCase], res: Resolution,
               explicit: Dict[str, str], skipped: set) -> Tuple[bool, List[str]]:
    """Skip, add keys for testKeys, and remove the entries that would update a matched test.
    Returns (changed, warnings)."""
    parents = {child: parent for parent in root.iter() for child in parent}
    changed = False
    stripped: Dict[str, List[str]] = {}
    renamed = 0
    for i, (c, key) in enumerate(zip(cases, res.keys)):
        if c.name in skipped:
            parent = parents.get(c.element)
            if parent is not None:
                parent.remove(c.element)
                changed = True
            continue
        if key and (c.name in explicit or fmt == "robot") and c.report_keys != [key]:
            # Robot: a definition match is pinned with the key tag too, so the name below
            # can change without losing the match.
            _inject_xml_key(c.element, fmt, key, c.report_keys)
            changed = True
        current = res.current[i] if fmt == "robot" and res.current else None
        if current is not None and c.element.get("name") != current:
            c.element.set("name", current)  # Xray would rename the test to this name
            changed = True
            renamed += 1
        if key:
            removed = _strip_updates(c.element, fmt)
            if removed:
                changed = True
                stripped.setdefault(c.name, removed)
    warnings = []
    if renamed:
        warnings.append("%d Robot test name(s) in the uploaded copy were set to the matched "
                        "tests' summaries, so Xray does not rename those tests (your file is "
                        "unchanged)." % renamed)
    if stripped:
        names = sorted({n for v in stripped.values() for n in v})
        warnings.append("Removed %s from %d case(s) matched to existing tests, because Xray "
                        "would overwrite those tests' fields (summary, description, labels) "
                        "with them: %s"
                        % (", ".join(names), len(stripped), ", ".join(list(stripped)[:20])))
    return changed, warnings


_EXEC_ID = """
query ResultExecId($jql: String!) { getTestExecutions(jql: $jql, limit: 1) { results { issueId } } }
"""


def _exec_test_keys(ctx, exec_key: str) -> List[str]:
    rows = ((ctx.xray.graphql(_EXEC_ID, {"jql": "issue = %s" % exec_key})
             .get("getTestExecutions") or {}).get("results")) or []
    if not rows:
        raise ToolError("Test execution not found: %s" % exec_key, testExecKey=exec_key)
    return _run_summary(ctx, str(rows[0]["issueId"]))[1]


def _exec_info(pk: str, summary: str, extra: Dict[str, Any], plan_key: Optional[str],
               envs: List[str]) -> dict:
    """The multipart `info` part: Jira fields of the new Test Execution plus xrayFields."""
    info: Dict[str, Any] = {"fields": {"project": {"key": pk}, "summary": summary,
                                       "issuetype": {"name": "Test Execution"}, **extra}}
    xf: Dict[str, Any] = {}
    if plan_key:
        xf["testPlanKey"] = plan_key
    if envs:
        xf["environments"] = envs
    if xf:
        info["xrayFields"] = xf
    return info


@REGISTRY.tool(
    "import_execution_results",
    "Import test results into Xray. format: xray | cucumber | behave (JSON) or junit | testng"
    " | nunit | robot (XML). Give exactly one of filePath (max 25 MB) or content. Without "
    "testExecKey a new Test Execution is created in projectKey; with it, results go to that "
    "execution. Each result is first matched to a test of projectKey (Cucumber/Behave by "
    "@TEST_<KEY> or the testKeyTag tag, else exact summary; XML as Xray does; Xray JSON by "
    "Generic definition). If a result matches several tests or none, nothing is uploaded and "
    "status is needs_answer with `questions`, `matched`, `ambiguous`, `missing`: show them to"
    " the user and call again with testKeys {name: KEY}, skipMissing, or allowCreateTests "
    "(only after the user agreed). Existing tests are never changed, except requirement links"
    " named in the file; the uploaded copy (never your file) is adjusted so Xray does not "
    "rename matched tests. Every key must belong to projectKey. Returns the execution key, "
    "created, and runs by status; detail adds the raw response.",
    _schema({"format": {"type": "string", "enum": list(FORMATS)},
             "filePath": {"type": "string", "description":
                          "Local results file; must not be empty. " + localfiles.PATH_NOTE},
             "content": {"type": "string", "description":
                         "Inline results; must not be empty."},
             "testExecKey": {"type": "string"}, "testPlanKey": {"type": "string"},
             "testEnvironments": {"type": "array", "items": {"type": "string"}},
             "summary": {"type": "string"}, "fields": _OBJ, "rawFields": _OBJ,
             "testKeys": {"type": "object", "additionalProperties": {"type": "string"},
                          "description": "If a @TEST_ tag and the testKeyTag tag name different tests the tool asks; "
                                         "answer here. Result name exactly as listed in needs_answer -> "
                                         "test key. Names: the scenario name (\"Feature: "
                                         "Scenario\" when features share it), the XML case "
                                         "definition, or the Xray JSON summary (\"summary "
                                         "(definition)\" when entries share it)"},
             "allowCreateTests": {"type": "boolean", "description":
                                  "Let new Test issues be created for results that match no "
                                  "test. Only with the user's consent. Default false. Created tests are listed in "
                                  "createdTests and get no AI label."},
             "skipMissing": {"type": "boolean", "description":
                             "Leave results that match no test out of the import."},
             "detail": {"type": "boolean"}, "remember": _REMEMBER}, ["format"]),
    write=True)
def import_execution_results(ctx, args):
    pk, fmt = args["projectKey"], args["format"]
    project = require_project(ctx, pk, write=True)
    if fmt not in FORMATS:
        raise ToolError("Unknown format '%s'" % fmt, formats=list(FORMATS))
    exec_key, plan_key = args.get("testExecKey"), args.get("testPlanKey")
    envs = _check_envs(args.get("testEnvironments") or [])
    for k in (exec_key, plan_key):
        if k:
            check_issue_in_project(k, pk)
    explicit = _check_test_keys(args.get("testKeys"), pk)
    allow_create, skip_missing = bool(args.get("allowCreateTests")), bool(args.get("skipMissing"))
    if allow_create and skip_missing:
        raise ToolError("Pass allowCreateTests or skipMissing, not both")
    data = _read_input(ctx, args)
    report: Any = None
    target_exec = exec_key
    if fmt == "xray":
        # Every key in the report must belong to the project, whatever the endpoint.
        report = _parse_json(data, fmt)
        report_exec = check_xray_report(report, pk)
        if exec_key and report_exec and report_exec != exec_key:
            raise ToolError("The report names testExecutionKey %s, not %s"
                            % (report_exec, exec_key))
        target_exec = exec_key or report_exec
    wants_fields = any(args.get(n) for n in ("summary", "fields", "rawFields"))
    if target_exec and wants_fields:
        raise ToolError("summary, fields and rawFields only apply when a new execution is "
                        "created; omit them or omit testExecKey")
    # Parse the results into cases (keys checked against the project), before any call.
    scen: List[rm.ScenarioCase] = []
    xml_cases: List[rm.XmlCase] = []
    xray_entries: List[Tuple[int, str, Optional[str]]] = []
    root = None
    if fmt in ("cucumber", "behave"):
        report = _parse_json(data, fmt)
        scen = rm.scenario_cases(report, fmt, pk, project.test_key_tag)
        if not scen:
            raise ToolError("The %s report has no scenarios" % fmt.capitalize())
    elif fmt in XML_FORMATS:
        root = rm.parse_xml(data, fmt)
        xml_cases = rm.xml_cases(root, fmt, pk)
    else:
        xray_entries = _xray_cases(report)
    # testKeys and scenario tag keys must name existing Tests: anything else would be a link
    # to Xray (a Robot tag), a dropped scenario or a failed upload, and the result could fall
    # back to Xray creating a test.
    known: Dict[str, str] = {}
    tag_keys = sorted({k for c in scen for k in c.tag_keys})
    if explicit or tag_keys:
        wanted = sorted(set(explicit.values()) | set(tag_keys))
        known = rm.existing_tests(ctx, wanted)
        bad = [k for k in wanted if k not in known]
        if bad:
            raise ToolError("testKeys and @TEST_/testKeyTag tags must name existing Tests of "
                            "%s; these are not: %s" % (pk, ", ".join(bad)), notTests=bad)
    # Match every result to a test (reads only).
    if scen:
        res = _resolve_scenarios(ctx, pk, scen, explicit)
    elif fmt in XML_FORMATS:
        res = _resolve_xml(ctx, pk, fmt, xml_cases, explicit, known)
    else:
        pre_keyed = sum(1 for t in (report.get("tests") or [])
                        if isinstance(t, dict) and t.get("testKey"))
        res = _resolve([n for _, n, _ in xray_entries], [[] for _ in xray_entries],
                       [d for _, _, d in xray_entries],
                       lambda ds: rm.find_by_definition(ctx, pk, ds)[0], explicit, pre_keyed)
        # Entries Xray would match by definition itself stay as they are.
    if res.ambiguous or (res.missing and not allow_create and not skip_missing):
        return _needs_answer(res, fmt)
    # Decide the endpoint and resolve the Jira fields of a new execution, after the matching
    # question (an elicited answer is then not lost to it) and before any write.
    multipart = False
    info: Optional[dict] = None
    applied: Dict[str, Any] = {}
    extra: Dict[str, Any] = {}
    if not target_exec:
        extra, applied = requiredfields.resolve(ctx, project, "testExecution", args.get("fields"),
                                                args.get("rawFields"),
                                                bool(args.get("remember", False)))
    if not target_exec and (wants_fields or extra or (project.fields.get("testExecution") or {})
                            or fmt in ("cucumber", "behave")):
        # A new execution with Jira fields; Cucumber/Behave always come this way, because
        # their plain endpoints take no project (the multipart info sets fields.project).
        multipart = True
        info = _exec_info(pk, args.get("summary") or "Imported results (%s)" % fmt, extra,
                          plan_key, envs)
    skipped = set(res.missing) if skip_missing else set()
    creating = allow_create and bool(res.missing)
    names = [c.name for c in scen] or [c.name for c in xml_cases] or \
        [n for _, n, _ in xray_entries]
    if skipped and all(n in skipped for n in names) and not (
            fmt == "xray" and len(xray_entries) < len(report.get("tests") or [])):
        raise ToolError("Nothing to import: no result matches a test of %s (skipMissing left "
                        "them all out)" % pk, skipped=sorted(skipped)[:CAP])
    # Build what is sent.
    sent_as = fmt
    warnings: List[str] = []
    if scen:
        if target_exec or creating:
            # Xray Cloud's Cucumber/Behave endpoints always create a new execution and never
            # create tests (verified live), so these go as Xray JSON.
            converted = scenarios_to_xray(scen, res.keys, pk, creating)
            if target_exec:
                converted = _with_target(converted, exec_key, plan_key, envs) or converted
            sent_as = "xray"
            data = json.dumps(converted).encode("utf-8")
        elif _tag_scenarios(scen, res.keys, fmt):
            data = json.dumps(report).encode("utf-8")
    elif fmt in XML_FORMATS:
        changed_xml, xml_warnings = _apply_xml(root, fmt, xml_cases, res, explicit, skipped)
        warnings.extend(xml_warnings)
        if changed_xml:
            data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    else:
        # Every entry with a known test goes by testKey alone: Xray overwrites a test's
        # summary and definition from testInfo, even next to a testKey (verified live).
        tests = report.get("tests") or []
        by_index = {i: (n, k) for (i, n, _), k in zip(xray_entries, res.keys)}
        new_tests = []
        dropped_info = 0
        for i, t in enumerate(tests):
            n, key = by_index.get(i, (None, None))
            if n is not None and n in skipped:
                continue
            if isinstance(t, dict) and t.get("testKey") and "testInfo" in t:
                t = {k: v for k, v in t.items() if k != "testInfo"}
                dropped_info += 1
            elif key:
                t = {k: v for k, v in t.items() if k != "testInfo"}
                t["testKey"] = key
            new_tests.append(t)
        if dropped_info:
            warnings.append("Removed testInfo from %d entr(ies) that have a testKey, because "
                            "Xray would overwrite those tests' summary and definition with it."
                            % dropped_info)
        rewritten = dict(report, tests=new_tests) if new_tests != tests else report
        changed = _with_target(rewritten, exec_key, plan_key, envs, project=pk)
        if changed is not None or rewritten is not report:
            data = json.dumps(changed or rewritten).encode("utf-8")
    # Tests already in the execution, so the ones Xray creates can be told apart.
    before: List[str] = _exec_test_keys(ctx, target_exec) if creating and target_exec else []
    ctype = "text/xml" if fmt in XML_FORMATS else "application/json"
    path = _endpoint(sent_as, multipart)
    requiredfields.commit(ctx)
    if multipart:
        # The results part is "results": verified live for JUnit, Cucumber and Xray JSON
        # (Cucumber with "result" gets HTTP 400 "Unexpected field (result)").
        resp = ctx.xray.rest("POST", path, multipart={"fields": {}, "files": {
            "info": ("info.json", json.dumps(info).encode("utf-8"), "application/json"),
            "results": ("results", data, ctype)}})
    else:
        params: Dict[str, str] = {}
        if fmt in XML_FORMATS:
            params["projectKey"] = pk
            if exec_key:
                params["testExecKey"] = exec_key
            if plan_key:
                params["testPlanKey"] = plan_key
            if envs:
                params["testEnvironments"] = ";".join(envs)
        kw: Dict[str, Any] = {"data": data, "content_type": ctype}
        if params:
            kw["params"] = params
        resp = ctx.xray.rest("POST", path, **kw)
    # Verified live (plain and multipart): Xray Cloud answers {"id", "key", "self"} of the
    # Test Execution; "testExecIssue" (Xray Server/DC shape) is accepted too.
    resp_d = resp if isinstance(resp, dict) else {}
    issue = resp_d.get("testExecIssue") if isinstance(resp_d.get("testExecIssue"), dict) \
        else resp_d
    key = issue.get("key") or target_exec
    out: Dict[str, Any] = {"testExecKey": key, "created": not target_exec}
    requiredfields.add_applied(out, "testExecution", applied)
    issue_id = issue.get("id")
    run_keys: Optional[List[str]] = None
    try:
        if not issue_id:
            raise ToolError("Xray did not return the test execution id")
        out["summary"], run_keys = _run_summary(ctx, str(issue_id))
    except Exception as e:
        out["summary"] = None
        warnings.append("Results were imported but the run summary could not be read: %s" % e)
    if creating:
        known = set(before) | {k for k in res.keys if k}
        if fmt == "xray":
            known |= {t.get("testKey") for t in (report.get("tests") or [])
                      if isinstance(t, dict) and t.get("testKey")}
        out["createdTests"] = None if run_keys is None else \
            dedupe([k for k in run_keys if k not in known])
        out["note"] = ("Tests created by this import carry no AI label; label them with "
                       "edit_test or set_lifecycle_label if wanted.")
    if skipped:
        out["skipped"] = sorted(skipped)[:CAP]
    if res.notes:
        warnings.extend(res.notes)
    if warnings:
        out["warnings"] = warnings
    if sent_as != fmt:
        out["sentAs"] = ("xray JSON converted from %s (one status per test, failures as "
                         "comment)" % fmt)
    if args.get("detail"):
        out["response"] = resp
    return out
