from __future__ import annotations

import json

import pytest

from conftest import FakeTransport, json_response, make_context
from xray_cloud.http import Response
from xray_cloud.preferences import PreferenceStore
from xray_cloud.registry import REGISTRY
import xray_cloud.tools  # noqa: F401

YAML = """
projects:
  P:
    fields:
      test:
        team: {id: customfield_1}
  Q: {}
"""
SECRETS = {"XRAY_CLIENT_ID": "id-SECRET-111", "XRAY_CLIENT_SECRET": "sec-SECRET-222",
           "JIRA_API_TOKEN": "tok-SECRET-333", "JIRA_EMAIL": "me-SECRET@x.io"}


def run(ctx, name, **args):
    res = REGISTRY.call(ctx, name, args)
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def make(tmp_path, yaml=YAML, **kw):
    ctx = make_context(yaml, tmp_path=tmp_path, **kw)
    ctx.preferences = PreferenceStore({"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")})
    return ctx


def stored(ctx, pk="P"):
    return ctx.preferences.get("global", pk)


def test_registration():
    sp, gs = REGISTRY.get("set_preferences"), REGISTRY.get("get_status")
    assert sp.write is False and gs.write is False
    assert "projectKey" in sp.params["required"]
    assert "projectKey" not in gs.params["properties"]
    assert "check" in gs.params["properties"]
    assert "example" in sp.description


def test_set_and_overwrite(tmp_path):
    ctx = make(tmp_path)
    res, err = run(ctx, "set_preferences", projectKey="P", createTestSet=True,
                   backgrounds="inline", aiLabel="")
    assert not err, res
    assert stored(ctx) == {"createTestSet": True, "backgrounds": "inline", "aiLabel": ""}
    assert res["effective"]["createTestSet"] == {"value": True, "source": "remembered"}
    assert res["effective"]["aiLabel"] == {"value": "", "source": "remembered"}
    assert res["effective"]["tagsAsLabels"] == {"source": "unanswered"}
    assert res["effective"]["unmatchedTags"] == {"value": "keep", "source": "default"}
    assert res["warnings"] == []
    run(ctx, "set_preferences", projectKey="P", createTestSet=False, aiLabel="ai-made")
    assert stored(ctx)["createTestSet"] is False and stored(ctx)["aiLabel"] == "ai-made"


def test_tag_rules_stored_raw(tmp_path):
    ctx = make(tmp_path)
    rules = [{"tag": "suite", "label": "{value}"}, {"tag": "owner", "field": {"team": "{value}"}}]
    res, err = run(ctx, "set_preferences", projectKey="P", tagRules=rules)
    assert not err, res
    assert stored(ctx)["tagRules"] == rules
    assert res["effective"]["tagRules"]["source"] == "remembered"
    run(ctx, "set_preferences", projectKey="P", tagRules=[])
    assert stored(ctx)["tagRules"] == []


def test_invalid_values_write_nothing(tmp_path):
    ctx = make(tmp_path)
    bad = [{"backgrounds": "x"}, {"aiLabel": "a b"}, {"aiLabelScope": "some"},
           {"unmatchedTags": "drop"},
           {"tagRules": [{"tag": "t", "field": {"nope": "{value}"}}]},
           {"tagRules": [{"tag": "t"}]}]
    for extra in bad:
        res, err = run(ctx, "set_preferences", projectKey="P", createTestSet=True, **extra)
        assert err, extra
    assert stored(ctx) == {}
    assert not ctx.preferences.path.exists()


def test_clear(tmp_path):
    ctx = make(tmp_path)
    run(ctx, "set_preferences", projectKey="P", createTestSet=True, tagsAsLabels=False)
    res, err = run(ctx, "set_preferences", projectKey="P", clear=["createTestSet"])
    assert not err, res
    assert stored(ctx) == {"tagsAsLabels": False}
    assert res["effective"]["createTestSet"] == {"source": "unanswered"}
    res, err = run(ctx, "set_preferences", projectKey="P", clear=["bogus"], backgrounds="inline")
    assert err and "bogus" in json.dumps(res)
    assert stored(ctx) == {"tagsAsLabels": False}  # nothing applied


def test_clear_and_set_same_name_is_refused(tmp_path):
    ctx = make(tmp_path)
    res, err = run(ctx, "set_preferences", projectKey="P", backgrounds="inline",
                   clear=["backgrounds"])
    assert err


def test_nothing_to_do_is_an_error(tmp_path):
    res, err = run(make(tmp_path), "set_preferences", projectKey="P")
    assert err


def test_settings_override_warns(tmp_path):
    yaml = YAML.replace("  Q: {}", "  Q: {}\nfeatureImport:\n  backgrounds: preconditions\n"
                                   "aiCreated:\n  label: team-ai\n")
    ctx = make(tmp_path, yaml)
    res, err = run(ctx, "set_preferences", projectKey="P", backgrounds="inline",
                   aiLabel="mine", createTestSet=True)
    assert not err, res
    assert stored(ctx)["backgrounds"] == "inline"  # still stored
    assert res["effective"]["backgrounds"] == {"value": "preconditions", "source": "settings"}
    assert res["effective"]["aiLabel"] == {"value": "team-ai", "source": "settings"}
    assert len(res["warnings"]) == 2
    text = " ".join(res["warnings"])
    assert "backgrounds" in text and "aiLabel" in text and "createTestSet" not in text


def test_not_blocked_by_read_only(tmp_path):
    ctx = make(tmp_path, read_only=True)
    res, err = run(ctx, "set_preferences", projectKey="P", createTestSet=True)
    assert not err, res
    assert stored(ctx) == {"createTestSet": True}


def test_unknown_project_refused(tmp_path):
    ctx = make(tmp_path)
    res, err = run(ctx, "set_preferences", projectKey="ZZ", createTestSet=True)
    assert err and "Unknown" in res["error"]


# ---- get_status -----------------------------------------------------------

def status_ctx(tmp_path, jira=True, **kw):
    ctx = make(tmp_path, jira=jira, **kw)
    ctx.env.update(SECRETS)
    ctx.env["XRAY_REGION"] = "eu"
    return ctx


def test_status_basics_and_no_secrets(tmp_path):
    from xray_cloud.config import JiraCredentials
    ctx = status_ctx(tmp_path)
    ctx.jira_creds = JiraCredentials("https://x.atlassian.net", SECRETS["JIRA_EMAIL"],
                                     SECRETS["JIRA_API_TOKEN"])
    ctx._creds = object()  # configured
    run(ctx, "set_preferences", projectKey="P", createTestSet=True)
    res, err = run(ctx, "get_status")
    assert not err, res
    assert res["version"] == "1.0.0"
    assert res["region"] == "eu" and res["baseUrl"] == "https://eu.xray.cloud.getxray.app"
    assert res["credentials"] == {"configured": True}
    assert res["settings"]["path"] and res["settings"]["error"] is None
    assert res["settings"]["searched"]
    assert res["projects"] == ["P", "Q"]
    assert res["readOnly"] is False
    assert res["jira"] == {"configured": True, "site": "https://x.atlassian.net"}
    assert res["preferencesFile"] == str(ctx.preferences.path)
    p = res["perProject"]["P"]
    assert p["featureImport"]["createTestSet"] == {"value": True, "source": "remembered"}
    assert p["featureImport"]["backgrounds"] == {"source": "unanswered"}
    assert p["aiCreated"]["aiLabel"] == {"source": "unanswered"}
    assert p["unanswered"] == ["backgrounds", "tagsAsLabels", "aiLabel", "aiLabelScope"]
    blob = json.dumps(res)
    for v in SECRETS.values():
        assert v not in blob


def test_status_without_credentials_or_jira(tmp_path):
    ctx = make(tmp_path)
    res, err = run(ctx, "get_status")
    assert not err, res
    assert res["credentials"] == {"configured": False}
    assert res["jira"] == {"configured": False, "site": None}
    assert res["region"] == "global"


def test_status_check_makes_one_myself_call(tmp_path):
    ctx = status_ctx(tmp_path)
    ft = FakeTransport(lambda c: json_response({"displayName": "Ann", "accountId": "a1",
                                                "emailAddress": "ann@x.io"}))
    ctx._transport = ft
    res, err = run(ctx, "get_status")  # no check: no call
    assert ft.calls == []
    res, err = run(ctx, "get_status", check=True)
    assert not err, res
    assert len(ft.calls) == 1 and ft.calls[0].url.endswith("/rest/api/3/myself")
    assert res["jira"]["reachable"] is True and res["jira"]["user"] == "Ann"
    assert SECRETS["JIRA_API_TOKEN"] not in json.dumps(res)


def test_status_check_failure_reported_not_raised(tmp_path):
    ctx = status_ctx(tmp_path)
    ctx._transport = FakeTransport(lambda c: Response(401, {}, b"{}"))
    res, err = run(ctx, "get_status", check=True)
    assert not err, res
    assert res["jira"]["reachable"] is False and "authentication" in res["jira"]["message"]


def test_status_check_without_jira_makes_no_call(tmp_path):
    ctx = make(tmp_path)
    res, err = run(ctx, "get_status", check=True)
    assert not err and res["jira"]["configured"] is False
    assert "reachable" not in res["jira"]


def test_status_broken_settings_shows_error(tmp_path):
    ctx = status_ctx(tmp_path, yaml="projects: [unclosed\n")
    res, err = run(ctx, "get_status")
    assert not err, res
    assert "settings.yml" in res["settings"]["error"]
    assert res["settings"]["path"].endswith("settings.yml")
    assert res["projects"] == [] and res["perProject"] == {}


def test_status_missing_explicit_settings_file(tmp_path):
    ctx = status_ctx(tmp_path)
    ctx.env["XRAY_SETTINGS_FILE"] = str(tmp_path / "nope.yml")
    res, err = run(ctx, "get_status")
    assert not err and "does not exist" in res["settings"]["error"]


def test_status_settings_source_and_read_only(tmp_path):
    yaml = YAML + "featureImport:\n  tagsAsLabels: false\n"
    ctx = status_ctx(tmp_path, yaml=yaml, read_only=True)
    res, err = run(ctx, "get_status")
    assert res["readOnly"] is True
    assert res["perProject"]["Q"]["featureImport"]["tagsAsLabels"] == \
        {"value": False, "source": "settings"}


def test_status_broken_preferences_file_reported(tmp_path):
    ctx = status_ctx(tmp_path)
    ctx.preferences.path.parent.mkdir(parents=True)
    ctx.preferences.path.write_text("{nope")
    res, err = run(ctx, "get_status")
    assert not err, res
    assert "not valid JSON" in res["preferencesError"]


def test_set_preferences_repairs_an_invalid_remembered_value(tmp_path):
    """Final fix 10: the error points to set_preferences, and that call fixes the entry."""
    from xray_cloud.choices import resolve
    ctx = make(tmp_path)
    ctx.preferences.update("global", "P", {"createTestSet": "yes"})
    res, err = run(ctx, "get_status")
    assert not err and "set_preferences" in res["preferencesError"]
    assert "createTestSet" in res["preferencesError"]
    res, err = run(ctx, "set_preferences", projectKey="P", createTestSet=False)
    assert not err, res
    assert resolve(ctx, "P", "createTestSet", None) == (False, "remembered")


def test_status_lists_result_import_test_key_tag(tmp_path):
    ctx = make(tmp_path, YAML.replace("  Q: {}", "  Q: {testKeyTag: issue}"))
    res, err = run(ctx, "get_status")
    assert not err, res
    assert res["perProject"]["Q"]["resultImport"] == {"testKeyTag": "issue"}
    assert res["perProject"]["P"]["resultImport"] == {"testKeyTag": None}


def _site_ctx(tmp_path):
    ctx = make(tmp_path)
    ctx.env = dict(ctx.env, XRAY_CLIENT_ID="id-SECRET-111", XRAY_CLIENT_SECRET="sec-SECRET-222")
    return ctx


def test_set_preferences_revoke_and_clear(tmp_path):
    ctx = _site_ctx(tmp_path)
    site = ctx.site
    assert site
    ctx.preferences.approve("global", site, "P", True)
    ctx.preferences.approve("global", site, "Q", False)
    ctx.preferences.remember_fields("global", site, "P", "test", {"customfield_12345": "Backend"})
    ctx.preferences.remember_fields("global", site, "P", "testPlan", {"customfield_2": "x"})
    res, err = run(ctx, "set_preferences", projectKey="P", revokeProject="Q", approveAll=False,
                   clearFieldValues={"itemType": "test"})
    assert not err
    assert ctx.preferences.approval("global", site) == {"approveAll": False,
                                                        "approvedProjects": ["P"]}
    assert ctx.preferences.field_values("global", site, "P", "test") == {}
    assert ctx.preferences.field_values("global", site, "P", "testPlan") == {"customfield_2": "x"}
    res, err = run(ctx, "set_preferences", projectKey="P", clearFieldValues={})
    assert not err and ctx.preferences.field_values("global", site, "P", "testPlan") == {}
    res, err = run(ctx, "set_preferences", projectKey="P", approveAll=True)
    assert err and "false" in res["error"]


def test_get_status_shows_approvals_and_field_values_without_site_id(tmp_path):
    ctx = _site_ctx(tmp_path)
    site = ctx.site
    ctx.preferences.approve("global", site, "P", False)
    ctx.preferences.remember_fields("global", site, "P", "test", {"customfield_12345": "Backend"})
    res, err = run(ctx, "get_status")
    assert not err
    per = res["perProject"]["P"]
    assert "approvals" not in per
    assert res["approvals"] == {"approveAll": False, "approvedProjects": ["P"]}
    assert per["fieldValues"] == {"test": {"customfield_12345": "Backend"}}
    assert site not in json.dumps(res)
    assert "SECRET" not in json.dumps(res)


def test_get_status_zero_config_shows_approvals_and_unlisted_field_values(tmp_path):
    ctx = make(tmp_path, yaml="")
    ctx.env = dict(ctx.env, XRAY_CLIENT_ID="id-SECRET-111", XRAY_CLIENT_SECRET="sec-SECRET-222")
    site = ctx.site
    ctx.preferences.approve("global", site, "PROJ", False)
    ctx.preferences.approve("global", site, None, True)
    ctx.preferences.remember_fields("global", site, "PROJ", "test", {"customfield_12345": "Backend"})
    res, err = run(ctx, "get_status")
    assert not err
    assert res["approvals"] == {"approveAll": True, "approvedProjects": ["PROJ"]}
    assert res["perProject"]["PROJ"]["fieldValues"] == {"test": {"customfield_12345": "Backend"}}


def test_clear_field_values_rejects_empty_item_type(tmp_path):
    res, err = run(make(tmp_path), "set_preferences", projectKey="P",
                   clearFieldValues={"itemType": ""})
    assert err


@pytest.mark.parametrize("args", [{"clearFieldValues": {"itemType": "plan"}},
                                  {"revokeProject": "not a key"}, {"revokeProject": "proj"}])
def test_set_preferences_rejects_unknown_item_type_and_bad_project_key(tmp_path, args):
    res, err = run(make(tmp_path), "set_preferences", projectKey="P", **args)
    assert err and ("itemType must be one of" in res["error"]
                    or "revokeProject must be a project key" in res["error"]), res
