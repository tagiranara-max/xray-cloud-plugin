from __future__ import annotations

from typing import Any, Dict, Mapping

from .errors import NeedsAnswer, ToolError

ALLOW_PROJECT_PARAM: Dict[str, Any] = {
    "type": "string", "enum": ["all", "project"],
    "description": "Only when a previous call returned needs_answer with question "
                   "allowProject: the user's answer. \"all\" allows writes to every project on "
                   "this Xray site from now on; \"project\" allows only this project. Never "
                   "answer it yourself; ask the user. Remembered per user."}

_ELICIT_DESCRIPTION = ("all = every project on this Xray site (remembered), project = only "
                       "this project (remembered), no = cancel this write")


def _region(ctx: Any) -> str:
    from .choices import region_of
    return region_of(ctx)


def _question_text(pk: str, region: str) -> str:
    return ("Allow the xray-cloud plugin to create and change Xray items in Jira project %s "
            "on your Xray Cloud site (region %s)? \"all\" allows writes to every project on "
            "this site from now on without asking again; \"project\" allows only %s. The "
            "answer is remembered for your user; revoke it with set_preferences."
            % (pk, region, pk))


def _refused(pk: str, why: str) -> ToolError:
    return ToolError("Writing to project %s was not approved (%s); nothing was written and "
                     "nothing was remembered." % (pk, why), projectKey=pk)


def _check_exists(ctx: Any, pk: str) -> None:
    """The project must exist in Xray before "this project" is remembered."""
    from .context import resolve_project_id
    try:
        resolve_project_id(ctx, pk)
    except ToolError as e:
        if e.data.get("reason") == "project_not_found":
            raise ToolError("Project %s was not found in Xray; check the project key. Nothing "
                            "was written and nothing was remembered." % pk, projectKey=pk)
        raise ToolError("Could not check that project %s exists in Xray (%s); nothing was "
                        "written and nothing was remembered." % (pk, e.message),
                        projectKey=pk, cause=e.message)


def _remember(ctx: Any, region: str, pk: str, answer: str) -> None:
    if answer == "project":
        _check_exists(ctx, pk)
        ctx.preferences.approve(region, ctx.site, pk, False)
    else:
        ctx.preferences.approve(region, ctx.site, None, True)


def is_approved(ctx: Any, pk: str) -> bool:
    """Whether the user approved `pk` (or all projects) on this Xray site."""
    site = ctx.site
    if not site:
        return False
    approved = ctx.preferences.approval(_region(ctx), site)
    return approved["approveAll"] or pk in approved["approvedProjects"]


def ensure_write_allowed(ctx: Any, pk: str, args: Mapping[str, Any]) -> None:
    """Returns when a write to project `pk` is allowed; raises NeedsAnswer (ask the user via
    the agent) or ToolError (refused). Order: read-only blocks; listed in the settings file;
    approved project or approve-all on this site; otherwise ask, by elicitation when the
    client supports it (the agent's allowProject is then ignored), else needs_answer."""
    from .context import NO_CREDENTIALS_MESSAGE, require_write
    require_write(ctx)
    if pk in ctx.settings_store.get().projects:
        return
    site = ctx.site
    if not site:
        raise ToolError(NO_CREDENTIALS_MESSAGE)
    region = _region(ctx)
    if is_approved(ctx, pk):
        return
    question = _question_text(pk, region)
    channel = getattr(ctx, "channel", None)
    if channel is not None and channel.supports_elicitation:
        content = channel.elicit(question, {
            "type": "object",
            "properties": {"approve": {"type": "string", "enum": ["all", "project", "no"],
                                       "default": "all", "description": _ELICIT_DESCRIPTION}},
            "required": ["approve"]})
        answer = content.get("approve") if isinstance(content, dict) else None
        if answer not in ("all", "project"):
            raise _refused(pk, "the user declined" if answer == "no" else "no answer")
        _remember(ctx, region, pk, answer)
        return
    answer = (args or {}).get("allowProject")
    if answer not in ("all", "project"):
        raise NeedsAnswer(
            "Writing to project %s needs the user's approval. Ask the user and call again "
            "with allowProject set to their answer; never answer it yourself." % pk,
            [{"id": "allowProject", "question": question, "choices": ["all", "project"],
              "default": "all", "context": {"projectKey": pk}}])
    _remember(ctx, region, pk, answer)
