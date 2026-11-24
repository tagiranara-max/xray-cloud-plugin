"""Which existing test each result of an import goes to, worked out before any upload.

Xray Cloud's own rules (verified live, docs/coverage.md "Result import matching"):
- Cucumber / Behave JSON: only a `@TEST_<KEY>` tag (Behave: `TEST_<KEY>`) maps a scenario;
  untagged scenarios are dropped silently (HTTP 400 "There are no valid tests imported" when
  none is tagged). No summary matching, no test creation.
- JUnit / TestNG / NUnit / Robot: an explicit key (JUnit property `test_key`, TestNG attribute
  `test`, NUnit property `Test`, Robot tag equal to the key) maps the case to that test; else an
  existing Generic test with the same definition is reused; else a new Generic test is created
  (summary = case name, definition as below). No summary matching.
- Xray JSON: an entry without `testKey` but with `testInfo` creates a test (Cucumber: always a new
  one; Generic: reuses the Generic test with the same definition and overwrites its summary).

This module parses result files into cases and runs the read-only lookups; tools/results.py
decides what to send.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .context import ISSUE_KEY_RE, ServerContext, check_issue_in_project, dedupe, jql_text
from .errors import ToolError

TEST_TAG = re.compile(r"^@TEST_([A-Z][A-Z0-9_]*-[1-9][0-9]*)$")
# Behave names Scenario Outline rows "<outline name> -- @<table>.<row> <examples name>".
_BEHAVE_ROW = re.compile(r"\s+--\s+@\d+\.\d+(?:\s.*)?$")
# cucumber-jvm / cucumber-js ids of Scenario Outline example rows end in ";<examples>;<row>"
_EXAMPLE_ROW_ID = re.compile(r";[^;]*;\d+$")


# ---- Cucumber / Behave ------------------------------------------------------------------------

@dataclass
class ScenarioCase:
    name: str                 # the name shown and used in testKeys: the summary, prefixed
                              # with "<Feature>: " when other features have the same name
    summary: str              # scenario name (outline rows: the outline's name), trimmed
    feature: str
    element: dict             # the report element (tags are rewritten in place when needed)
    tag_keys: List[str]       # keys named by @TEST_ / testKeyTag tags, sorted, unique
    outline: bool
    background: List[dict] = field(default_factory=list)  # the Background element before it


def _key_tag_re(tag: Optional[str]):
    if not tag:
        return None
    return re.compile(r"^@%s[=:_]([A-Z][A-Z0-9_]*-[1-9][0-9]*)$" % re.escape(tag))


def tag_names(element: dict, fmt: str) -> List[str]:
    """The element's tags as '@name' strings (Behave writes them as plain strings, no '@')."""
    out = []
    for t in element.get("tags") or []:
        name = t if isinstance(t, str) else (t or {}).get("name") if isinstance(t, dict) else None
        if isinstance(name, str) and name:
            out.append(name if name.startswith("@") else "@" + name)
    return out


def is_background(el: dict) -> bool:
    return el.get("type") == "background" or \
        str(el.get("keyword") or "").strip().lower() == "background"


def is_outline_row(el: dict) -> bool:
    keyword = str(el.get("keyword") or "").strip().lower()
    return keyword in ("scenario outline", "scenario template") or \
        bool(_EXAMPLE_ROW_ID.search(str(el.get("id") or "")))


def scenario_name(el: dict, feature: str, fmt: str) -> str:
    """The summary a test for this scenario has: its trimmed name (Behave outline rows lose
    their ' -- @1.1 ...' suffix); an unnamed scenario is '<Feature> (line N)', as
    create_tests_from_feature names it."""
    name = str(el.get("name") or "").strip()
    if fmt == "behave":
        name = _BEHAVE_ROW.sub("", name).strip()
    if name:
        return name
    return "%s (line %s)" % (feature.strip() or "Scenario", el.get("line") or "?")


def scenario_cases(report: Any, fmt: str, pk: str, key_tag: Optional[str]) -> List[ScenarioCase]:
    """Every scenario (or outline row) of a Cucumber/Behave JSON report, in order. Keys named by
    tags must belong to `pk`."""
    if not isinstance(report, list):
        raise ToolError("%s JSON results must be a list of features" % fmt.capitalize())
    custom = _key_tag_re(key_tag)
    out: List[ScenarioCase] = []
    for feature in report:
        if not isinstance(feature, dict):
            raise ToolError("%s JSON results must be a list of feature objects"
                            % fmt.capitalize())
        fname = str(feature.get("name") or feature.get("uri") or "")
        background: List[dict] = []
        for el in feature.get("elements") or []:
            if not isinstance(el, dict):
                continue
            if is_background(el):
                background = [el]
                continue
            keys = []
            for t in tag_names(el, fmt):
                m = TEST_TAG.match(t) or (custom.match(t) if custom else None)
                if m:
                    check_issue_in_project(m.group(1), pk)
                    keys.append(m.group(1))
            summary = scenario_name(el, fname, fmt)
            out.append(ScenarioCase(summary, summary, fname, el, sorted(set(keys)),
                                    is_outline_row(el), background))
            background = []
    # The same scenario name in several features: name each result after its feature, so
    # testKeys and skipMissing act per result (the summary lookup still uses the bare name).
    features: Dict[str, set] = {}
    for c in out:
        features.setdefault(c.summary, set()).add(c.feature)
    for c in out:
        if len(features[c.summary]) > 1:
            c.name = "%s: %s" % (c.feature or "?", c.summary)
    return out


# ---- XML (JUnit, TestNG, NUnit, Robot) --------------------------------------------------------

@dataclass
class XmlCase:
    name: str                 # the Generic test definition Xray derives (unique per case)
    summary: str              # the summary Xray gives a test it creates
    element: Any              # the ET element of the case
    report_keys: List[str]    # keys the file itself names


def parse_xml(data: bytes, fmt: str):
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise ToolError("The %s results are not valid XML: %s" % (fmt, e)) from None


def _props(el, container: str, item: str, name: str, attr_value: bool) -> List[str]:
    out = []
    for box in el.findall(container):
        for p in box.findall(item):
            if p.get("name") == name:
                v = p.get("value") if attr_value else p.text
                if v and v.strip():
                    out.append(v.strip())
    return out


def _xml_items(root, fmt: str) -> List[Tuple[str, str, Any, List[str]]]:
    """(definition, summary, element, keys named in the file) per case."""
    items: List[Tuple[str, str, Any, List[str]]] = []
    if fmt == "junit":
        for tc in root.iter("testcase"):
            name, cls = tc.get("name") or "", tc.get("classname") or ""
            items.append(("%s.%s" % (cls, name) if cls else name, name, tc,
                          _props(tc, "properties", "property", "test_key", True)))
    elif fmt == "testng":
        for cls in root.iter("class"):
            for m in cls.findall("test-method"):
                if (m.get("is-config") or "").lower() == "true":
                    continue
                name = m.get("name") or ""
                items.append(("%s.%s" % (cls.get("name") or "", name), name, m,
                              _props(m, "attributes", "attribute", "test", False)))
    elif fmt == "nunit":
        for tc in root.iter("test-case"):
            name = tc.get("name") or ""
            items.append((tc.get("fullname") or name, name, tc,
                          _props(tc, "properties", "property", "Test", True)))
    elif fmt == "robot":
        def walk(suite, chain: List[str]) -> None:
            chain = chain + [suite.get("name") or ""]
            for child in suite:
                if child.tag == "suite":
                    walk(child, chain)
                elif child.tag == "test":
                    name = child.get("name") or ""
                    keys = [t.text.strip() for box in child.findall("tags")
                            for t in box.findall("tag")
                            if t.text and ISSUE_KEY_RE.fullmatch(t.text.strip())]
                    items.append((".".join(chain + [name]), name, child, keys))
        for suite in ([root] if root.tag == "suite" else root.findall("suite")):
            walk(suite, [])
    return items


def xml_cases(root, fmt: str, pk: str) -> List[XmlCase]:
    """The cases of a JUnit/TestNG/NUnit/Robot report. Keys the file names must belong to `pk`.
    Robot: every key-shaped tag is only a candidate (dates like ISO-8601, requirement links);
    which are Tests, and whether those are in `pk`, is checked later with existing_tests."""
    out = []
    for definition, summary, el, keys in _xml_items(root, fmt):
        if fmt != "robot":
            for k in keys:
                check_issue_in_project(k, pk)
        out.append(XmlCase(definition, summary, el, sorted(set(keys))))
    return out


# ---- read-only lookups ------------------------------------------------------------------------

PAGE = 100                  # Xray's page size limit
SUMMARY_CHUNK = 10          # names OR-ed into one `summary ~` query
MAX_SUMMARY_PAGES = 20      # fuzzy hits checked per query before giving up
MAX_DEFINITION_PAGES = 50   # Generic tests scanned for definitions (5000)

_SUMMARIES = """
query ResultMatchSummaries($jql: String!, $limit: Int!, $start: Int!) {
  getTests(jql: $jql, limit: $limit, start: $start) {
    total results { issueId jira(fields: ["key", "summary"]) }
  }
}
"""
_DEFINITIONS = """
query ResultMatchDefinitions($jql: String!, $testType: TestTypeInput, $limit: Int!, $start: Int!) {
  getTests(jql: $jql, testType: $testType, limit: $limit, start: $start) {
    total results { issueId unstructured jira(fields: ["key"]) }
  }
}
"""
_KEYS = """
query ResultMatchKeys($jql: String!, $limit: Int!) {
  getTests(jql: $jql, limit: $limit) { results { issueId jira(fields: ["key", "summary"]) } }
}
"""


def _run_pages(ctx: ServerContext, query: str, variables: dict,
               max_pages: int) -> Tuple[List[dict], bool]:
    """(rows of every page, complete): pages getTests until `total`; complete is False when
    max_pages pages were not enough."""
    rows: List[dict] = []
    start = 0
    for _ in range(max_pages):
        data = (ctx.xray.graphql(query, dict(variables, limit=PAGE, start=start))
                .get("getTests")) or {}
        page = data.get("results") or []
        rows.extend(page)
        start += len(page)
        if not page or start >= (data.get("total") or 0):
            return rows, True
    return rows, False


def _summary_query(ctx, pk: str, chunk: List[Tuple[str, str]]) -> Optional[Dict[str, List[str]]]:
    """{name: keys} for one OR-ed query; None when it has too many fuzzy hits to check."""
    jql = "project = %s AND (%s)" % (pk, " OR ".join("summary ~ %s" % t for _, t in chunk))
    rows, complete = _run_pages(ctx, _SUMMARIES, {"jql": jql}, MAX_SUMMARY_PAGES)
    if not complete:
        return None
    wanted = {n for n, _ in chunk}
    found: Dict[str, List[str]] = {n: [] for n in wanted}
    for r in rows:
        j = r.get("jira") or {}
        key, summary = str(j.get("key") or ""), str(j.get("summary") or "").strip()
        if key.startswith(pk + "-") and summary in wanted:
            found[summary].append(key)
    return found


def find_by_summary(ctx: ServerContext, pk: str, names: Sequence[str]) -> Dict[str, List[str]]:
    """{name: keys of the project's tests whose summary is exactly the name} (both trimmed,
    case-sensitive). `summary ~` is fuzzy, so hits are checked here; names are OR-ed ten per
    query. A name with nothing searchable maps to []."""
    out: Dict[str, List[str]] = {}
    terms: List[Tuple[str, str]] = []
    for n in dedupe([n.strip() for n in names]):
        t = jql_text(n)
        if t is None:
            out[n] = []
        else:
            terms.append((n, t))
    for i in range(0, len(terms), SUMMARY_CHUNK):
        chunk = terms[i:i + SUMMARY_CHUNK]
        found = _summary_query(ctx, pk, chunk)
        if found is None:  # too many fuzzy hits together: one name at a time
            found = {}
            for one in chunk:
                single = _summary_query(ctx, pk, [one])
                if single is None:
                    raise ToolError(
                        "Cannot tell which test has the summary %r: more than %d tests in %s "
                        "match it in a text search; nothing was imported. Name its test with "
                        "testKeys or a @TEST_<KEY> tag" % (one[0], MAX_SUMMARY_PAGES * PAGE, pk),
                        name=one[0])
                found.update(single)
        for n, keys in found.items():
            out[n] = dedupe(keys)
    return out


def find_by_definition(ctx: ServerContext, pk: str,
                       definitions: Sequence[str]) -> Tuple[Dict[str, List[str]], bool]:
    """({definition: keys of the project's Generic tests with exactly that definition},
    complete). Scans the project's Generic tests (at most MAX_DEFINITION_PAGES pages);
    complete is False when there were more."""
    wanted = set(definitions)
    out: Dict[str, List[str]] = {d: [] for d in definitions}
    rows, complete = _run_pages(ctx, _DEFINITIONS, {"jql": "project = %s" % pk,
                                                    "testType": {"name": "Generic"}},
                                MAX_DEFINITION_PAGES)
    for r in rows:
        key = str((r.get("jira") or {}).get("key") or "")
        d = r.get("unstructured")
        if d in wanted and key.startswith(pk + "-") and key not in out[d]:
            out[d].append(key)
    return out, complete


def _test_keys(ctx: ServerContext, keys: List[str]) -> Dict[str, str]:
    data = ctx.xray.graphql(_KEYS, {"jql": "issue in (%s)" % ", ".join(keys), "limit": PAGE})
    out: Dict[str, str] = {}
    for r in ((data.get("getTests") or {}).get("results")) or []:
        j = r.get("jira") or {}
        if j.get("key"):
            out[j["key"]] = str(j.get("summary") or "")
    return out


_NO_SUCH_ISSUE = re.compile(r"does not exist|is invalid|not valid|no project", re.IGNORECASE)


def existing_tests(ctx: ServerContext, keys: Sequence[str]) -> Dict[str, str]:
    """{key: summary} of the keys that are Test issues (keys must be key-shaped). JQL rejects
    a whole `issue in (...)` when one key does not exist, so such a chunk is retried key by
    key; a key JQL still rejects is not a test. Other errors are raised."""
    found: Dict[str, str] = {}
    keys = list(dict.fromkeys(keys))
    for i in range(0, len(keys), PAGE):
        chunk = keys[i:i + PAGE]
        try:
            found.update(_test_keys(ctx, chunk))
        except ToolError as e:
            if not _NO_SUCH_ISSUE.search(str(e)):
                raise
            for k in chunk:
                try:
                    found.update(_test_keys(ctx, [k]))
                except ToolError as e2:
                    if not _NO_SUCH_ISSUE.search(str(e2)):
                        raise
    return found
