"""Shared wording for things the server cannot do itself (spec 6.6): the `notDone` entries of
the feature import and the no-token errors of the Jira-only tools use the same reason and fix."""
from __future__ import annotations

from typing import Dict

NO_JIRA_REASON = "no Jira token or Atlassian MCP"
NO_JIRA_FIX = ("set the Jira token in plugin settings, or let the agent apply them via the "
               "Atlassian MCP")
NEVER_OVERWRITE_FIX = "existing values are never overwritten; change it in Jira if wanted"
UNREADABLE_REASON = "current value could not be read; not changed"
UNREADABLE_FIX = "check the field id in the settings file; set it in Jira if wanted"


def not_done(item: str, what: str, reason: str = NO_JIRA_REASON,
             fix: str = NO_JIRA_FIX) -> Dict[str, str]:
    return {"item": item, "what": what, "reason": reason, "fix": fix}
