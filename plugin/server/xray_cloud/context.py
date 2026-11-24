from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .config import (PROJECT_KEY_RE, Credentials, JiraCredentials, ProjectSettings,
                     SettingsStore)
from .errors import ToolError
from .http import Transport, urllib_transport
from .jira_client import JiraClient
from .preferences import PreferenceStore, site_id
from .ratelimit import RateLimiter
from .stepcache import StepCache, TTLCache
from .xray_client import XrayClient

NO_CREDENTIALS_MESSAGE = (
    "Xray credentials are not configured. Set them in Claude Code's plugin "
    "settings (/plugin -> xray-cloud -> configure), or as the XRAY_CLIENT_ID and "
    "XRAY_CLIENT_SECRET environment variables in your MCP client's server config.")


class ServerContext:
    """Shared state for tool handlers. Construction never fails on missing credentials."""

    def __init__(self, env: Mapping[str, str], settings_store: SettingsStore,
                 creds: Optional[Credentials], jira_creds: Optional[JiraCredentials] = None,
                 read_only: bool = False, transport: Optional[Transport] = None,
                 limiter: Optional[RateLimiter] = None,
                 xray: Optional[XrayClient] = None) -> None:
        self.env = env
        self.settings_store = settings_store
        self.read_only = read_only
        self.preferences = PreferenceStore(env)
        self.jira_creds = jira_creds
        self._jira: Optional[Any] = None
        self._jira_set = False
        self.step_cache = StepCache()
        self.folder_cache = TTLCache()  # (projectId, path) -> getFolder data
        self.project_ids: Dict[str, str] = {}
        self._creds = creds
        self._transport = transport or urllib_transport
        self._limiter = limiter or RateLimiter()
        self._xray = xray
        # ClientChannel for server-to-client requests (elicitation); serve() attaches it.
        self.channel: Optional[Any] = None
        # Arguments of the tool call being handled that handlers do not see (allowProject);
        # set by Registry.call.
        self.call_args: Dict[str, Any] = {}
        # Jira create-meta per (project, item type) for this server run (requiredfields).
        self.create_meta_cache: Dict[Any, Any] = {}
        # Required-field answers of the current call, remembered on requiredfields.commit.
        self.pending_fields: Dict[Any, Any] = {}

    @property
    def site(self) -> str:
        """Hash of the Xray client id scoping approvals and remembered values; never shown."""
        return site_id(self.env)

    @property
    def has_credentials(self) -> bool:
        """Whether Xray credentials were configured (never exposes them)."""
        return self._creds is not None

    @property
    def jira(self) -> Optional[Any]:
        """JiraClient built lazily from the optional Jira credentials; None without them."""
        if not self._jira_set and self._jira is None and self.jira_creds is not None:
            self._jira = JiraClient(self.jira_creds, self._transport)
        return self._jira

    @jira.setter
    def jira(self, value: Optional[Any]) -> None:
        self._jira, self._jira_set = value, True

    @property
    def xray(self) -> XrayClient:
        if self._xray is None:
            if self._creds is None:
                raise ToolError(NO_CREDENTIALS_MESSAGE)
            self._xray = XrayClient(self._creds, self._transport, self._limiter)
        return self._xray


class PartialFailure(ToolError):
    pass


def partial_failure(message: str, created: List[str], cause: BaseException,
                    **data: Any) -> PartialFailure:
    """A PartialFailure listing `created`, with the cause text and permission enrichment."""
    err = PartialFailure(message, created=list(created), cause=str(cause))
    err.data.update(data)
    return with_permission_reason(err, cause)


def with_permission_reason(err: ToolError, cause: BaseException) -> ToolError:
    """Add reason=permission_denied (and permission=DELETE_ISSUES when known) to `err` when
    its cause was a permission error. Returns `err`."""
    if isinstance(cause, ToolError) and cause.data.get("reason") == "permission_denied":
        err.data["reason"] = "permission_denied"
        if cause.data.get("permission"):
            err.data["permission"] = cause.data["permission"]
    elif is_permission_error(cause):
        err.data["reason"] = "permission_denied"
        if _matches(cause, _DELETE_RE):
            err.data["permission"] = "DELETE_ISSUES"
    return err


# Xray/Jira wording for a missing project permission, e.g. "You do not have permission to
# delete issues in this project" (verified live); HTTP 403 counts as well.
_PERMISSION_RE = re.compile(r"(do not|don't|does not|doesn't) have (the )?permission"
                            r"|permission denied|not authori[sz]ed to", re.IGNORECASE)
_DELETE_RE = re.compile(r"permission to delete", re.IGNORECASE)
# "Issue does not exist or you do not have permission to see it": usually a wrong key.
_NOT_FOUND_RE = re.compile(r"not found|does not exist|doesn't exist"
                           r"|permission to (view|see|browse)", re.IGNORECASE)
ADMIN_HINT = ("Ask a Jira admin to grant you %s (Project settings \u2192 Permissions) or the "
              "project Administrator role.")


def _texts(exc: ToolError) -> List[str]:
    return [exc.message] + [str(e.get("message", "")) if isinstance(e, dict) else str(e)
                            for e in exc.data.get("graphqlErrors") or []]


def _matches(exc: Exception, pattern: "re.Pattern") -> bool:
    return isinstance(exc, ToolError) and any(pattern.search(t) for t in _texts(exc))


def is_permission_error(exc: BaseException) -> bool:
    """Permission text (case-insensitive) or HTTP 403; not-found/visibility wording and
    already-mapped errors are excluded."""
    if not isinstance(exc, ToolError) or exc.data.get("reason") == "permission_denied":
        return False
    if _matches(exc, _NOT_FOUND_RE):
        return False
    return exc.data.get("status") == 403 or _matches(exc, _PERMISSION_RE)


def permission_denied(exc: ToolError, *, action: str, project_key: Optional[str],
                      key: Optional[str] = None) -> ToolError:
    """A clear, stable error for a missing permission; the original message is kept as
    xrayMessage. Delete Issues is named only for action "delete" when Xray's text says so
    (a bare HTTP 403 gets the generic wording)."""
    where = "project %s" % project_key if project_key else "this project"
    data: Dict[str, Any] = {"reason": "permission_denied"}
    if action == "delete" and _matches(exc, _PERMISSION_RE):
        data["permission"] = "DELETE_ISSUES"
        message = ("You don't have permission to delete issues in %s. " % where
                   + ADMIN_HINT % "'Delete Issues'")
    else:
        data["action"] = action
        message = ("You don't have permission to %s in %s. " % (action, where)
                   + ADMIN_HINT % "the required project permission")
    data["projectKey"] = project_key
    if key is not None:
        data["key"] = key
    data["xrayMessage"] = exc.message
    return ToolError(message, **data)


@contextmanager
def permission_guard(action: str, project_key: Optional[str],
                     key: Optional[str] = None) -> Iterator[None]:
    """Re-raise permission errors from the wrapped calls as permission_denied; others unchanged."""
    try:
        yield
    except ToolError as e:
        if is_permission_error(e):
            raise permission_denied(e, action=action, project_key=project_key, key=key) from None
        raise


def require_write(ctx: ServerContext) -> None:
    if ctx.read_only:
        raise ToolError("Plugin is in read-only mode")


def require_project(ctx: ServerContext, project_key: str, *, write: bool) -> ProjectSettings:
    """Settings of a project; empty settings for a project the settings file does not list.
    Writes go through approval (approval.ensure_write_allowed, with the call's allowProject
    from ctx.call_args). Reads are allowed when there is no settings file, the file lists no
    projects, the project is listed, or it is approved (or approve-all) on this site."""
    # Format first: on the no-settings-file read path the key goes raw into JQL.
    if not isinstance(project_key, str) or not PROJECT_KEY_RE.fullmatch(project_key):
        raise ToolError("Invalid project key", projectKey=project_key)
    settings = ctx.settings_store.get()
    if write:
        from .approval import ensure_write_allowed
        ensure_write_allowed(ctx, project_key, getattr(ctx, "call_args", None) or {})
        return settings.projects.get(project_key) or ProjectSettings(project_key, fields={})
    project = settings.projects.get(project_key)
    if project is not None:
        return project
    if settings.path is None or not settings.projects:
        return ProjectSettings(project_key, fields={})
    from .approval import is_approved
    if is_approved(ctx, project_key):
        return ProjectSettings(project_key, fields={})
    raise ToolError("Unknown Xray project key", known=sorted(settings.projects),
                    hint="%s is not listed in the settings file %s. Writing to it asks the user "
                         "for approval; once approved it can be read too."
                         % (project_key, settings.path))


_JQL_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("\t", "\\t"), ("\n", "\\n"), ("\r", "\\r"))
# Characters that break a Jira text search (`~`) phrase, e.g. "[" (verified live: a summary
# with brackets is not found by its own exact text); replaced by spaces.
_TEXT_SPECIAL = re.compile(r'[\[\]{}()^~*?:\\/!+&|"\s]+')


def jql_str(value: str) -> str:
    """A quoted JQL string literal (backslash, quote and control characters escaped)."""
    for a, b in _JQL_ESCAPES:
        value = value.replace(a, b)
    return '"%s"' % value


def jql_text(value: str) -> Optional[str]:
    """A quoted phrase for `summary ~`: Lucene-special characters and whitespace runs become
    single spaces. None when nothing searchable is left."""
    term = _TEXT_SPECIAL.sub(" ", value).strip()
    return '"%s"' % term if term else None


ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")


def check_issue_key(issue_key: str) -> None:
    """Format check only (keys are interpolated into JQL): uppercase project, '-', number >= 1."""
    if not isinstance(issue_key, str) or not ISSUE_KEY_RE.fullmatch(issue_key):
        raise ToolError("Invalid issue key", key=issue_key)


def check_issue_in_project(issue_key: str, project_key: str) -> None:
    check_issue_key(issue_key)
    if issue_key.rsplit("-", 1)[0] != project_key:
        raise ToolError("Issue %s does not belong to project %s" % (issue_key, project_key),
                        issueKey=issue_key, projectKey=project_key)


def dedupe(seq: List[Any]) -> List[Any]:
    """seq without duplicates, first occurrence kept."""
    return list(dict.fromkeys(seq))


def confirm_or_preview(confirm: bool, preview: dict) -> Optional[dict]:
    if confirm:
        return None
    return {"wouldDelete": preview, "hint": "pass confirm: true"}


def delete_preview(ctx: ServerContext, confirm: bool, preview: dict,
                   project_key: str) -> Optional[dict]:
    """confirm_or_preview, plus canDelete (Delete Issues permission) when a Jira token is
    configured; the check is skipped silently without a token or when it fails."""
    out = confirm_or_preview(confirm, preview)
    if out is None or ctx.jira is None:
        return out
    try:
        out["canDelete"] = bool(ctx.jira.has_permission(project_key, "DELETE_ISSUES"))
    except Exception:
        pass
    return out


_PROJECT_SETTINGS = ("query GetProjectSettings($projectIdOrKey: String) "
                     "{ getProjectSettings(projectIdOrKey: $projectIdOrKey) { projectId } }")
_PROJECT_ID_FROM_TESTS = ("query GetProjectId($jql: String!) { getTests(jql: $jql, limit: 1) "
                          "{ results { projectId } } }")


def _settings_project_id(ctx: ServerContext, project_key: str) -> Optional[str]:
    """projectId from getProjectSettings (works for a project without tests); None when Xray
    does not return one. The result may be an object or a one-element list."""
    try:
        data = ctx.xray.graphql(_PROJECT_SETTINGS, {"projectIdOrKey": project_key})
    except ToolError:
        return None
    res = data.get("getProjectSettings")
    if isinstance(res, list):
        res = res[0] if res else None
    pid = res.get("projectId") if isinstance(res, dict) else None
    if isinstance(pid, list):
        pid = pid[0] if pid else None
    return str(pid) if pid else None


def resolve_project_id(ctx: ServerContext, project_key: str) -> str:
    """Numeric Xray project id for a (validated) project key: getProjectSettings first, then
    a getTests lookup as fallback. Cached per server."""
    cached = ctx.project_ids.get(project_key)
    if cached:
        return cached
    pid = _settings_project_id(ctx, project_key)
    if pid is None:
        data = ctx.xray.graphql(_PROJECT_ID_FROM_TESTS, {"jql": 'project = "%s"' % project_key})
        results = ((data.get("getTests") or {}).get("results")) or []
        if results and results[0].get("projectId"):
            pid = str(results[0]["projectId"])
    if pid is None:
        raise ToolError("Could not determine the numeric Xray project id for %s" % project_key,
                        projectKey=project_key, reason="project_not_found")
    ctx.project_ids[project_key] = pid
    return pid


_COMPACT_KEYS = ("key", "summary", "status", "testType", "labels")


def compact(issue: dict, detail: bool) -> dict:
    """detail=True returns the issue unchanged; otherwise only the keys
    key, summary, status, testType, labels (the last three only when present)."""
    if detail:
        return issue
    return {k: issue[k] for k in _COMPACT_KEYS if k in issue}
