from __future__ import annotations

from ..context import check_issue_in_project, require_project
from ..errors import ToolError
from ..registry import REGISTRY
from .tests import _schema, fetch_test, require_jira


@REGISTRY.tool(
    "set_lifecycle_label",
    "Set a lifecycle label (from the plugin settings, default ai-draft, needs-refinement, "
    "automation-candidate, automated, obsolete) on a test, replacing any previous lifecycle "
    "label and keeping other labels. Labels are Jira fields, so this needs the optional Jira "
    "API token.",
    _schema({"testKey": {"type": "string"}, "label": {"type": "string"}}, ["testKey", "label"]),
    write=True)
def set_lifecycle_label(ctx, args):
    pk, key, label = args["projectKey"], args["testKey"], args["label"]
    require_project(ctx, pk, write=True)
    check_issue_in_project(key, pk)
    allowed = ctx.settings_store.get().lifecycle_labels
    if label not in allowed:
        raise ToolError("Unknown lifecycle label: %s" % label, allowed=list(allowed))
    jira = require_jira(ctx)
    current = (fetch_test(ctx, pk, key).get("jira") or {}).get("labels") or []
    new = [l for l in current if l not in allowed] + [label]
    jira.edit_issue(key, {"labels": new})
    return {"key": key, "labels": new, "lifecycleLabel": label}
