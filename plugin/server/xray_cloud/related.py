"""Helpers for analysing related tests: latest run status per test."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

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
    """Test issue id -> {status, on, testExecKey} of its most recent run. Tests that never
    ran are absent. Queried in chunks of 50 ids, up to 10 pages of 100 runs per chunk."""
    out: Dict[str, Dict[str, Any]] = {}
    ids = [str(i) for i in test_ids]
    for c in range(0, len(ids), CHUNK):
        chunk = ids[c:c + CHUNK]
        for page in range(MAX_PAGES):
            data = ctx.xray.graphql(_LATEST, {"testIssueIds": chunk, "limit": PAGE,
                                              "start": page * PAGE})
            rows = ((data.get("getTestRuns") or {}).get("results")) or []
            for r in rows:
                tid = str((r.get("test") or {}).get("issueId") or "")
                on = run_on(r)
                if not tid or not on:
                    continue
                cur = out.get(tid)
                if cur is None or on > cur["on"]:
                    out[tid] = {"status": (r.get("status") or {}).get("name"), "on": on,
                                "testExecKey": ((r.get("testExecution") or {}).get("jira")
                                                or {}).get("key")}
            if len(rows) < PAGE:
                break
    return out
