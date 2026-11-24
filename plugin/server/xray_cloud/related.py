"""Helpers for analysing related tests: latest run status per test."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .context import check_issue_key, jql_text, resolve_project_id
from .errors import ToolError
from .folderops import normalize_folder_path

PAGE = 100
CHUNK = 50
MAX_PAGES = 10

_LATEST = """
query LatestTestStatuses($testIssueIds: [String], $limit: Int!, $start: Int!) {
  getTestRuns(testIssueIds: $testIssueIds, limit: $limit, start: $start) {
    total
    results {
      status { name }
      startedOn
      finishedOn
      test { issueId }
      testExecution { jira(fields: ["key"]) }
    }
  }
}
"""


def run_on(run: dict) -> Optional[str]:
    """When a run happened: finishedOn, else startedOn."""
    return run.get("finishedOn") or run.get("startedOn")


def latest_statuses(ctx, test_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Test issue id -> {status, on, testExecKey} of its most recent run (see
    latest_statuses_checked, which also reports truncation)."""
    return latest_statuses_checked(ctx, test_ids)[0]


def latest_statuses_checked(ctx, test_ids: List[str]) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """Like latest_statuses, plus a flag that is True when a page cap was hit (answers may
    then be incomplete). Tests that never ran are absent; a run with no timestamp counts
    with on=None and loses to any timestamped run. Chunks of 50 ids, up to 10 pages of 100
    runs per chunk."""
    out: Dict[str, Dict[str, Any]] = {}
    truncated = False
    ids = [str(i) for i in test_ids]
    for c in range(0, len(ids), CHUNK):
        chunk = ids[c:c + CHUNK]
        for page in range(MAX_PAGES):
            data = ctx.xray.graphql(_LATEST, {"testIssueIds": chunk, "limit": PAGE,
                                              "start": page * PAGE})
            rows = ((data.get("getTestRuns") or {}).get("results")) or []
            for r in rows:
                tid = str((r.get("test") or {}).get("issueId") or "")
                if not tid:
                    continue
                on = run_on(r)
                cur = out.get(tid)
                if cur is None or (on or "") > (cur["on"] or ""):
                    out[tid] = {"status": (r.get("status") or {}).get("name"), "on": on,
                                "testExecKey": ((r.get("testExecution") or {}).get("jira")
                                                or {}).get("key")}
            if len(rows) < PAGE:
                break
        else:
            truncated = True
    return out, truncated


MAX_RELATED = 200
KINDS = ("terms", "issueKeys", "labels", "components", "folder")

_TESTS_BY_JQL = """
query RelatedTests($jql: String!, $limit: Int!, $start: Int!) {
  getTests(jql: $jql, limit: $limit, start: $start) {
    total
    results { issueId folder { path } jira(fields: ["key", "summary", "labels"]) }
  }
}
"""
_TESTS_BY_FOLDER = """
query RelatedTestsInFolder($projectId: String!, $path: String!, $limit: Int!, $start: Int!) {
  getTests(projectId: $projectId, folder: {path: $path, includeDescendants: true}, limit: $limit, start: $start) {
    total
    results { issueId folder { path } jira(fields: ["key", "summary", "labels"]) }
  }
}
"""


@dataclass
class Criteria:
    terms: List[str] = field(default_factory=list)
    issue_keys: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    folder: Optional[str] = None

    def empty(self) -> bool:
        return not (self.terms or self.issue_keys or self.labels or self.components
                    or self.folder)


def _quote(value: str) -> str:
    """A JQL string literal (backslash and double quote escaped)."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError("Empty value in criteria", value=value)
    return '"%s"' % value.strip().replace("\\", "\\\\").replace('"', '\\"')


def _jql_for(kind: str, values: List[str], pk: str) -> Optional[str]:
    """The project-scoped JQL for one criterion kind (None when nothing searchable remains)."""
    if kind == "terms":
        parts = ["text ~ %s" % t for t in (jql_text(v) for v in values) if t]
    elif kind == "issueKeys":
        for v in values:
            check_issue_key(v)
        parts = ['issue in linkedIssues("%s", "is tested by")' % v for v in values]
    elif kind == "labels":
        parts = ["labels in (%s)" % ", ".join(_quote(v) for v in values)] if values else []
    elif kind == "components":
        parts = ["component in (%s)" % ", ".join(_quote(v) for v in values)] if values else []
    else:
        raise ToolError("Unknown criterion kind", kind=kind)
    if not parts:
        return None
    return 'project = "%s" AND (%s)' % (pk, " OR ".join(parts))


def _fetch(ctx, query: str, base: Dict[str, Any], limit: int) -> Tuple[List[dict], bool]:
    """Page one query up to `limit` rows; the flag is True when more rows existed."""
    rows: List[dict] = []
    capped = False
    while len(rows) < limit:
        want = min(PAGE, limit - len(rows))
        data = ctx.xray.graphql(query, dict(base, limit=want, start=len(rows)))
        found = data.get("getTests") or {}
        got = found.get("results") or []
        rows.extend(got)
        total = found.get("total")
        if len(got) < want or (isinstance(total, int) and len(rows) >= total):
            return rows, False
    capped = True
    return rows, capped


def find_related(ctx, pk: str, criteria: Criteria, limit: int,
                 with_truncated: bool = False):
    """Tests of project `pk` matching any criterion, merged by key: [{key, summary, folder,
    labels, lastStatus, matchedBy}], most matches first then key, at most `limit` (<= 200)
    rows. With with_truncated=True returns (rows, truncated)."""
    if criteria.empty():
        raise ToolError("Provide at least one criterion: terms, issueKeys, labels, "
                        "components or folder")
    limit = max(1, min(int(limit), MAX_RELATED))
    queries: List[Tuple[str, str, Dict[str, Any]]] = []
    for kind, values in (("terms", criteria.terms), ("issueKeys", criteria.issue_keys),
                         ("labels", criteria.labels), ("components", criteria.components)):
        if values:
            jql = _jql_for(kind, values, pk)
            if jql:
                queries.append((kind, _TESTS_BY_JQL, {"jql": jql}))
    if criteria.folder:
        path = normalize_folder_path(criteria.folder)
        queries.append(("folder", _TESTS_BY_FOLDER,
                        {"projectId": resolve_project_id(ctx, pk), "path": path}))
    if not queries:
        raise ToolError("No searchable criteria (terms contained only special characters)")
    merged: Dict[str, Dict[str, Any]] = {}
    ids: Dict[str, str] = {}
    truncated = False
    for kind, query, base in queries:
        rows, capped = _fetch(ctx, query, base, limit)
        truncated = truncated or capped
        for r in rows:
            jira = r.get("jira") or {}
            key = jira.get("key")
            if not key or not str(key).startswith(pk + "-"):
                continue
            row = merged.get(key)
            if row is None:
                row = merged[key] = {
                    "key": key, "summary": jira.get("summary"),
                    "folder": (r.get("folder") or {}).get("path"),
                    "labels": jira.get("labels") or [], "lastStatus": None, "matchedBy": []}
                ids[key] = str(r.get("issueId") or "")
            if kind not in row["matchedBy"]:
                row["matchedBy"].append(kind)
    ordered = sorted(merged.values(), key=lambda x: (-len(x["matchedBy"]), x["key"]))
    if len(ordered) > limit:
        truncated = True
        ordered = ordered[:limit]
    wanted = [ids[r["key"]] for r in ordered if ids.get(r["key"])]
    if wanted:
        latest, capped = latest_statuses_checked(ctx, wanted)
        truncated = truncated or capped
        for r in ordered:
            st = latest.get(ids.get(r["key"], ""))
            r["lastStatus"] = st["status"] if st else None
    return (ordered, truncated) if with_truncated else ordered
