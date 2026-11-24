"""Helpers for analysing related tests: latest run status per test."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

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
