"""Live tests against a real Xray Cloud tenant (marker `live`, excluded by default).

Run: XRAY_CLIENT_ID=... XRAY_CLIENT_SECRET=... XRAY_REGION=... XRAY_TEST_PROJECT=KEY
     [JIRA_SITE_URL=... JIRA_EMAIL=... JIRA_API_TOKEN=...] python3 -m pytest -m live -q

Read tests need only the Xray credentials; write tests need XRAY_TEST_PROJECT (a throwaway
project); Jira checks need the JIRA_* variables; the tag-rule field checks need
XRAY_TEST_DATE_FIELD (the id of a date field on the project's Test screen, else skipped).
Everything a test creates carries SUMMARY_PREFIX + the run id in its summary and is deleted at the end of the session; keys that
could not be deleted (or that the user may not delete) are reported as pytest warnings.
Issues are tracked as soon as a tool reports them, including the `created` keys of a failed
(partial) call. Not cleaned up: the test environment "xray-cloud-live" (Xray has no API to
delete environments; one fixed name is reused). Secrets are never printed.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import warnings

import pytest

import xray_cloud.tools  # noqa: F401  (registers every tool)
from xray_cloud.config import (SettingsStore, load_credentials, load_jira_credentials)
from xray_cloud.context import ServerContext, is_permission_error
from xray_cloud.errors import ToolError
from xray_cloud.registry import REGISTRY

SUMMARY_PREFIX = "[xray-cloud live test]"
# Put on every issue the suite creates where the create path takes labels, so leftovers
# (e.g. when the user lacks Delete Issues) can be found and bulk-deleted:
# JQL `labels = xray-cloud-live-test`.
LIVE_LABEL = "xray-cloud-live-test"
_CREATE_KINDS = {"create_test": "test", "create_test_set": "testSet",
                 "create_test_plan": "testPlan", "create_test_execution": "testExecution"}
ENV_NOTE = 'test environment "xray-cloud-live" is kept (Xray has no API to delete environments)'
_LABELLED_CREATES = ("create_test", "create_test_set", "create_test_plan",
                     "create_test_execution", "create_tests_from_feature")
RUN_ID = "xcl" + time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.live)


def _has(*names: str) -> bool:
    return all((os.environ.get(n) or "").strip() for n in names)


HAS_XRAY = _has("XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET")
HAS_PROJECT = HAS_XRAY and _has("XRAY_TEST_PROJECT")
HAS_JIRA = _has("JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")



class ToolFailed(AssertionError):
    def __init__(self, name: str, request_data: dict) -> None:
        super().__init__("%s failed: %s" % (name, json.dumps(request_data, ensure_ascii=False)[:1500]))
        self.request_data = request_data


class Live:
    """A ServerContext built from the environment (same path as main.py) plus a tracker of
    everything created, cleaned up in reverse order at session end."""

    def __init__(self, root: Path) -> None:
        self.project = (os.environ.get("XRAY_TEST_PROJECT") or "").strip()
        settings = root / "settings.yml"
        settings.write_text("projects:\n  %s: {}\n" % (self.project or "LIVE"), encoding="utf-8")
        env = dict(os.environ)
        env["XRAY_SETTINGS_FILE"] = str(settings)
        env["HOME"] = str(root / "home")
        env["CLAUDE_PROJECT_DIR"] = str(root / "project")
        # remembered import choices go to a temp dir, never the user's real preferences
        env["CLAUDE_PLUGIN_DATA"] = str(root / "plugin-data")
        env.pop("XRAY_READ_ONLY", None)
        (root / "project").mkdir()
        self.root = root
        self.env = env
        self.ctx = ServerContext(env, SettingsStore(env), load_credentials(env),
                                 load_jira_credentials(env), False)
        self.created: List[Tuple[str, str]] = []  # (kind, key)
        self.folders: List[str] = []
        self.denied: List[str] = []  # keys/folders the user may not delete (cleanup)
        self.run_id = RUN_ID

    @staticmethod
    def summary(text: str) -> str:
        return "%s %s %s" % (SUMMARY_PREFIX, RUN_ID, text)

    # -- tools ------------------------------------------------------------
    def call(self, name: str, **args: Any) -> dict:
        args.setdefault("projectKey", self.project)
        if name in _LABELLED_CREATES and "rawFields" not in args:
            args["rawFields"] = {"labels": [LIVE_LABEL]}
        res = REGISTRY.call(self.ctx, name, args)
        request_data = json.loads(res["content"][0]["text"])
        self._track_request(name, request_data)  # before raising: partial failures created issues
        if res.get("isError"):
            raise ToolFailed(name, request_data)
        return request_data

    def _track_request(self, name: str, request_data: dict) -> None:
        """Track every issue a tool reports as created (never `updated` ones: those may be
        pre-existing). Kind is known for create_* results, else looked up at cleanup."""
        if not isinstance(request_data, dict):
            return
        if name in _CREATE_KINDS and request_data.get("created") is True and request_data.get("key"):
            self.track(_CREATE_KINDS[name], request_data["key"])
        for field in ("created", "createdTests"):
            keys = request_data.get(field)
            if isinstance(keys, list):
                self.track("auto", *[k for k in keys if isinstance(k, str)])

    def call_error(self, name: str, **args: Any) -> dict:
        args.setdefault("projectKey", self.project)
        res = REGISTRY.call(self.ctx, name, args)
        assert res.get("isError"), "expected %s to fail: %s" % (name, res)
        return json.loads(res["content"][0]["text"])

    def gql(self, query: str, variables: Optional[dict] = None) -> dict:
        return self.ctx.xray.graphql(query, variables or {})

    def track(self, kind: str, *keys: str) -> None:
        for k in keys:
            if not k:
                continue
            known = [i for i, (_, v) in enumerate(self.created) if v == k]
            if not known:
                self.created.append((kind, k))
            elif kind != "auto" and self.created[known[0]][0] == "auto":
                self.created[known[0]] = (kind, k)

    def untrack(self, key: str) -> None:
        self.created = [(k, v) for k, v in self.created if v != key]

    # -- Jira REST (only with JIRA_* set) ---------------------------------------
    def jira(self, method: str, path: str, body: Any = None, params: Optional[dict] = None):
        client = self.ctx.jira
        assert client is not None, "Jira credentials not configured"
        return client._request(method, path, "%s %s" % (method, path), params=params,
                               json_body=body)

    def issue_id(self, key: str) -> Optional[str]:
        for q in ("getTests", "getPreconditions", "getTestSets", "getTestPlans",
                  "getTestExecutions"):
            data = self.gql('query { %s(jql: "issue = %s", limit: 1) { results { issueId } } }'
                            % (q, key))
            rows = (data.get(q) or {}).get("results") or []
            if rows:
                return rows[0]["issueId"]
        return None

    # -- cleanup ------------------------------------------------------------
    _DELETE = {"test": "deleteTest", "precondition": "deletePrecondition",
               "testSet": "deleteTestSet", "testPlan": "deleteTestPlan",
               "testExecution": "deleteTestExecution"}
    _QUERY = {"test": "getTests", "precondition": "getPreconditions", "testSet": "getTestSets",
              "testPlan": "getTestPlans", "testExecution": "getTestExecutions"}

    def _kind_of(self, key: str) -> Optional[str]:
        for kind, q in self._QUERY.items():
            rows = ((self.gql('query { %s(jql: "issue = %s", limit: 1) { results { issueId } } }'
                              % (q, key)).get(q) or {}).get("results")) or []
            if rows:
                return kind
        return "jira" if self.ctx.jira is not None else None

    def _delete(self, kind: str, key: str) -> None:
        if kind == "auto":
            kind = self._kind_of(key)
            if kind is None:
                raise ToolError("cannot determine the type of %s (no Jira token)" % key)
        if kind in self._DELETE:
            q = self._QUERY[kind]
            rows = ((self.gql('query { %s(jql: "issue = %s", limit: 1) { results { issueId } } }'
                              % (q, key)).get(q) or {}).get("results")) or []
            if not rows:
                return  # already gone
            self.gql("mutation D($id: String!) { %s(issueId: $id) }" % self._DELETE[kind],
                     {"id": rows[0]["issueId"]})
        else:  # plain Jira issue (e.g. the requirement Story)
            self.jira("DELETE", "/rest/api/3/issue/%s" % key)

    def cleanup(self) -> List[str]:
        """Delete everything tracked. Issues refused for lack of permission are collected in
        self.denied (not a failure); other errors are returned."""
        failed: List[str] = []
        self.denied: List[str] = []
        for kind, key in reversed(self.created):
            try:
                self._delete(kind, key)
            except ToolError as e:
                if is_permission_error(e):
                    self.denied.append(key)
                else:
                    failed.append("%s %s (%s)" % (kind, key, str(e)[:200]))
            except Exception as e:  # keep going; report at the end
                failed.append("%s %s (%s)" % (kind, key, str(e)[:200]))
        for path in reversed(self.folders):
            try:
                self.call("delete_folder", path=path, confirm=True)
            except ToolFailed as e:
                if e.request_data.get("reason") == "permission_denied":
                    self.denied.append("folder " + path)
                elif "not found" not in str(e).lower():
                    failed.append("folder %s (%s)" % (path, str(e)[:200]))
        return failed

    def leftovers(self) -> List[str]:
        """Issues of this run still findable by summary (JQL text search on the run id)."""
        jql = 'project = %s AND summary ~ "%s"' % (self.project, RUN_ID)
        if self.ctx.jira is not None:
            data = self.jira("GET", "/rest/api/3/search/jql",
                             params={"jql": jql, "fields": "summary", "maxResults": 100})
            return [i["key"] for i in (data or {}).get("issues") or []]
        keys: List[str] = []
        for q in self._QUERY.values():
            data = self.gql('query L($jql: String!) { %s(jql: $jql, limit: 100) '
                            '{ results { jira(fields: ["key"]) } } }' % q, {"jql": jql})
            keys += [r["jira"]["key"] for r in (data.get(q) or {}).get("results") or []]
        return keys


@pytest.fixture(scope="session")
def live(tmp_path_factory):
    if not HAS_XRAY:
        pytest.skip("XRAY_CLIENT_ID/SECRET not set")
    lv = Live(tmp_path_factory.mktemp("live"))
    yield lv
    if not lv.project:
        return
    failed = lv.cleanup()
    try:
        left = lv.leftovers()
    except Exception as e:
        left = ["(leftover check failed: %s)" % str(e)[:200]]
    hint = "JQL: labels = %s, or summary ~ \"%s\"" % (LIVE_LABEL, RUN_ID)
    if lv.denied:
        msg = ("LIVE LEFTOVERS (run %s): no permission to delete %d issue(s): %s. Delete them "
               "manually (%s). Also: %s."
               % (RUN_ID, len(lv.denied), ", ".join(lv.denied), hint, ENV_NOTE))
        print("\n" + msg)
        warnings.warn(msg)
        left = [k for k in left if k not in lv.denied]
    if failed or left:
        msg = ("LIVE CLEANUP INCOMPLETE (run %s): failed=%s leftovers=%s (%s). Also: %s."
               % (RUN_ID, failed, left, hint, ENV_NOTE))
        print("\n" + msg)
        warnings.warn(msg)
    elif not lv.denied:
        print("\nlive cleanup ok (run %s, %d issues deleted; %s)"
              % (RUN_ID, len(lv.created), ENV_NOTE))


@pytest.fixture
def wlive(live):
    """For write tests: needs XRAY_TEST_PROJECT."""
    if not HAS_PROJECT:
        pytest.skip("XRAY_TEST_PROJECT not set")
    return live


@pytest.fixture
def jlive(wlive):
    """For tests that also need the optional Jira token."""
    if not HAS_JIRA:
        pytest.skip("JIRA_SITE_URL/JIRA_EMAIL/JIRA_API_TOKEN not set")
    return wlive


@pytest.fixture(scope="session")
def results_log():
    """Collects verification outcomes; written to $XRAY_LIVE_LOG (if set) for docs/coverage.md."""
    log: Dict[str, Any] = {}
    yield log
    out = os.environ.get("XRAY_LIVE_LOG")
    if out:
        Path(out).write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")


def tmp_dir() -> str:
    return tempfile.mkdtemp(prefix="xray-live-")
