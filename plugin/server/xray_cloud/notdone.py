"""Shared wording for things the server cannot do itself (spec 6.6): the `notDone` entries of
the feature import and the no-token errors of the Jira-only tools use the same reason and fix."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from .config import jira_config_state

NO_JIRA_REASON = "no Jira token or Atlassian MCP"
NO_JIRA_FIX = ("set the Jira token in Claude Code's plugin settings, or as the JIRA_SITE_URL, "
              "JIRA_EMAIL and JIRA_API_TOKEN environment variables in your MCP client's server "
              "config, or let the agent apply them via the Atlassian MCP")
def jira_reason_fix(env: Mapping[str, Any]) -> Tuple[str, str]:
    """Reason and fix for work that needs Jira access: the generic no-token wording, or, when
    Jira is partly configured, which parts are missing."""
    state = jira_config_state(env)
    if state["set"] and state["missing"]:
        return ("Jira access not configured: %s missing" % ", ".join(state["missing"]),
                "Jira needs the site URL, the email of the account the token belongs to, and the "
                "token; add the missing part(s), or let the agent apply the edits via the "
                "Atlassian MCP")
    return NO_JIRA_REASON, NO_JIRA_FIX


NEVER_OVERWRITE_FIX = "existing values are never overwritten; change it in Jira if wanted"
UNREADABLE_REASON = "current value could not be read; not changed"
UNREADABLE_FIX = "check the field id in the settings file; set it in Jira if wanted"


def not_done(item: str, what: str, reason: str = NO_JIRA_REASON,
             fix: str = NO_JIRA_FIX) -> Dict[str, str]:
    return {"item": item, "what": what, "reason": reason, "fix": fix}
