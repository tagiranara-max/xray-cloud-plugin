from __future__ import annotations

import os
import time

import pytest

from xray_cloud.gherkin.tagrules import TagRule
from xray_cloud.config import (
    REGIONS,
    Settings,
    SettingsError,
    SettingsStore,
    find_settings_file,
    settings_search_paths,
    load_credentials,
    load_jira_credentials,
    load_read_only,
)

GOOD = """\
projects:
  PROJ:
    fields:
      test:
        component:
          id: customfield_12345
          type: option
          required: true
          default: Backend
      testRun:
        version:
          name: Version
lifecycleLabels: [a, b]
duplicateCheck: false
"""

BAD_YAML = "projects:\n  PROJ: {}\n\tbad: x\n"


def env(**kw):
    base = {"XRAY_CLIENT_ID": "cid", "XRAY_CLIENT_SECRET": "sec"}
    for k, v in kw.items():
        base[{"region": "XRAY_REGION"}.get(k, k)] = v
    return base


def store_for(tmp_path, text, name="settings.yml"):
    p = tmp_path / name
    p.write_text(text)
    return SettingsStore({"XRAY_SETTINGS_FILE": str(p)})


def test_userconfig_value_wins_over_env_fallback():
    c = load_credentials({"XRAY_CLIENT_ID": "plugin", "XRAY_CLIENT_SECRET": "s"})
    assert c.client_id == "plugin" and c.client_secret == "s" and c.region == "global"


def test_empty_userconfig_string_falls_back_to_shell_env():
    assert load_credentials({"XRAY_CLIENT_ID": "", "XRAY_CLIENT_SECRET": "s"}) is None
    assert load_credentials({"XRAY_CLIENT_ID": "${user_config.xray_client_id}",
                             "XRAY_CLIENT_SECRET": "s"}) is None
    assert load_credentials({}) is None


def test_region_maps_to_base_url():
    assert load_credentials(env(region="eu")).base_url == REGIONS["eu"]
    assert load_credentials(env(region="")).base_url == REGIONS["global"]
    assert load_credentials(env(region="${user_config.xray_region}")).region == "global"


def test_unknown_region_is_error():
    with pytest.raises(SettingsError, match=r"global.*eu.*au"):
        load_credentials(env(region="mars"))


def test_jira_credentials():
    e = {"JIRA_SITE_URL": "https://x.atlassian.net", "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t"}
    j = load_jira_credentials(e)
    assert (j.site_url, j.email, j.token) == ("https://x.atlassian.net", "a@b.c", "t")
    assert load_jira_credentials(dict(e, JIRA_EMAIL="")) is None
    assert load_jira_credentials(dict(e, JIRA_API_TOKEN="${user_config.jira_api_token}")) is None


def test_read_only():
    for v in ("true", "TRUE", "1", "Yes"):
        assert load_read_only({"XRAY_READ_ONLY": v}) is True
    for v in ("false", "0", "no", "", "${user_config.read_only}"):
        assert load_read_only({"XRAY_READ_ONLY": v}) is False
    assert load_read_only({}) is False
    with pytest.raises(SettingsError):
        load_read_only({"XRAY_READ_ONLY": "maybe"})


def test_lookup_order_first_found_wins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "xray-cloud").mkdir(parents=True)
    (home / ".config" / "xray-cloud" / "settings.yml").write_text("projects: {HOME: {}}")
    monkeypatch.setenv("HOME", str(home))
    explicit = tmp_path / "x.json"
    explicit.write_text('{"projects": {"EXPL": {}}}')

    assert find_settings_file({}) == home / ".config" / "xray-cloud" / "settings.yml"
    assert list(SettingsStore({}).get().projects) == ["HOME"]
    e = {"XRAY_SETTINGS_FILE": str(explicit)}
    assert list(SettingsStore(e).get().projects) == ["EXPL"]


def test_no_settings_file_anywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert find_settings_file({}) is None
    assert SettingsStore({}).get().path is None


def test_project_folder_settings_file_is_ignored(tmp_path, monkeypatch):
    """.xray/settings.yml in the project folder (or the cwd) is never read."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    (proj / ".xray").mkdir(parents=True)
    (proj / ".xray" / "settings.yml").write_text("projects: {PROJ: {}}")
    monkeypatch.chdir(proj)
    for env in ({}, {"CLAUDE_PROJECT_DIR": str(proj)}, {"XRAY_PROJECT_DIR": str(proj)}):
        assert find_settings_file(env) is None, env
        assert all(".xray" not in p.parts for p in settings_search_paths(env)), env
        assert SettingsStore(env).get().projects == {}, env


def test_explicit_settings_file_missing_is_an_error(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".xray").mkdir(parents=True)
    (proj / ".xray" / "settings.yml").write_text("projects: {PROJ: {}}")
    missing = tmp_path / "missing.yml"
    e = {"CLAUDE_PROJECT_DIR": str(proj), "XRAY_SETTINGS_FILE": str(missing)}
    with pytest.raises(SettingsError) as ei:
        SettingsStore(e).get()
    assert "XRAY_SETTINGS_FILE" in ei.value.message and str(missing) in ei.value.message
    assert ei.value.data["file"] == str(missing)
    e["XRAY_SETTINGS_FILE"] = str(tmp_path)  # a directory is not a settings file either
    with pytest.raises(SettingsError):
        find_settings_file(e)


def test_no_file_gives_empty_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = SettingsStore({}).get()
    assert s.projects == {} and s.path is None


def test_parses_good_file(tmp_path):
    s = store_for(tmp_path, GOOD).get()
    f = s.projects["PROJ"].fields["test"]["component"]
    assert (f.id, f.type, f.required, f.default) == ("customfield_12345", "option", True, "Backend")
    r = s.projects["PROJ"].fields["testRun"]["version"]
    assert r.xray_name == "Version" and r.id is None and r.required is False
    assert s.lifecycle_labels == ["a", "b"] and s.duplicate_check is False


def test_testrun_accepts_id_and_others_require_id(tmp_path):
    ok = "projects:\n  P:\n    fields:\n      testRun:\n        v: {id: abc}\n"
    assert store_for(tmp_path, ok).get().projects["P"].fields["testRun"]["v"].id == "abc"
    bad = "projects:\n  P:\n    fields:\n      test:\n        v: {name: Version}\n"
    with pytest.raises(SettingsError, match="id"):
        store_for(tmp_path, bad).get()


def test_yaml_syntax_error_names_file_and_line(tmp_path):
    with pytest.raises(SettingsError, match=r"settings\.yml.*line 3"):
        store_for(tmp_path, BAD_YAML).get()


def test_unknown_item_type_is_error(tmp_path):
    with pytest.raises(SettingsError, match="testExec"):
        store_for(tmp_path, "projects:\n  P:\n    fields:\n      testExec: {}\n").get()


def test_unknown_field_type_is_error(tmp_path):
    with pytest.raises(SettingsError, match="type"):
        store_for(tmp_path, "projects:\n  P:\n    fields:\n      test:\n        c: {id: x, type: blob}\n").get()


def test_credential_like_keys_rejected(tmp_path):
    for key in ("client_secret", "apiToken", "password", "clientId", "my_secret_thing"):
        text = "projects:\n  P: {}\n%s: SUPERSECRETVALUE\n" % key
        with pytest.raises(SettingsError) as ei:
            store_for(tmp_path, text).get()
        assert key in str(ei.value) and "SUPERSECRETVALUE" not in str(ei.value)


def test_reload_on_mtime_change(tmp_path):
    st = store_for(tmp_path, "projects: {A: {}}")
    assert list(st.get().projects) == ["A"]
    assert st.get() is st.get()
    p = tmp_path / "settings.yml"
    p.write_text("projects: {B: {}}")
    t = time.time() + 5
    os.utime(p, (t, t))
    assert list(st.get().projects) == ["B"]


def test_json_accepted(tmp_path):
    s = store_for(tmp_path, '{"projects": {"J": {}}}', name="settings.json").get()
    assert list(s.projects) == ["J"]


def test_default_lifecycle_labels():
    assert Settings.empty().lifecycle_labels == [
        "ai-draft", "needs-refinement", "automation-candidate", "automated", "obsolete"]
    assert Settings.empty().duplicate_check is True


def test_non_mapping_fields_is_settings_error(tmp_path):
    for val in ("[a]", "foo"):
        with pytest.raises(SettingsError, match=r"settings\.yml.*projects\.P\.fields"):
            store_for(tmp_path, "projects:\n  P:\n    fields: %s\n" % val).get()


def test_non_utf8_file_is_settings_error(tmp_path):
    p = tmp_path / "settings.yml"
    p.write_bytes(b"projects: {\xff\xfe: {}}\n")
    with pytest.raises(SettingsError, match=r"settings\.yml.*UTF-8"):
        SettingsStore({"XRAY_SETTINGS_FILE": str(p)}).get()


def test_booleans_must_be_real_booleans(tmp_path):
    with pytest.raises(SettingsError, match="duplicateCheck"):
        store_for(tmp_path, 'duplicateCheck: "false"\n').get()
    with pytest.raises(SettingsError, match="required"):
        store_for(tmp_path, 'projects:\n  P:\n    fields:\n      test:\n        c: {id: x, required: "false"}\n').get()


def test_credentials_repr_hides_client_secret():
    c = load_credentials({"XRAY_CLIENT_ID": "cid", "XRAY_CLIENT_SECRET": "very-secret-value"})
    assert "very-secret-value" not in repr(c) and "cid" in repr(c)


@pytest.mark.parametrize("yaml_text,expect", [
    ("projects:\n  proj: {}\n", "invalid project key 'proj'"),
    ("projects:\n  NO: {}\n", "boolean false"),
    ("projects:\n  ON: {}\n", "boolean true"),
    ("projects:\n  123: {}\n", "invalid project key 123"),
    ("projects:\n  PROJ-X: {}\n", "invalid project key 'PROJ-X'"),
])
def test_invalid_project_keys_in_settings_rejected(tmp_path, yaml_text, expect):
    p = tmp_path / "settings.yml"
    p.write_text(yaml_text)
    with pytest.raises(SettingsError) as ei:
        SettingsStore({"XRAY_SETTINGS_FILE": str(p)}).get()
    assert expect in ei.value.message and "settings.yml" in ei.value.message


def test_quoted_boolean_like_project_key_accepted(tmp_path):
    p = tmp_path / "settings.yml"
    p.write_text('projects:\n  "NO": {}\n  ABC_2: {}\n')
    assert list(SettingsStore({"XRAY_SETTINGS_FILE": str(p)}).get().projects) == ["NO", "ABC_2"]


# --- featureImport / aiCreated -------------------------------------------------------------

CHOICES_YAML = """\
featureImport:
  createTestSet: true
  backgrounds: inline
  tagRules: [{tag: a, label: "{value}"}]
aiCreated:
  label: ai-made
  scope: all
projects:
  P1:
    featureImport: {tagsAsLabels: false, unmatchedTags: skip}
    aiCreated: {label: null, scope: tests}
  P2: {}
"""


def test_feature_import_and_ai_created_parsed(tmp_path):
    s = store_for(tmp_path, CHOICES_YAML).get()
    rules = s.feature_import.pop("tagRules")
    assert len(rules) == 1 and isinstance(rules[0], TagRule) and rules[0].keys == ["a"]
    assert s.feature_import == {"createTestSet": True, "backgrounds": "inline"}
    assert s.ai_created == {"label": "ai-made", "scope": "all"}
    assert s.projects["P1"].feature_import == {"tagsAsLabels": False, "unmatchedTags": "skip"}
    assert s.projects["P1"].ai_created == {"label": None, "scope": "tests"}
    assert s.projects["P2"].feature_import == {} and s.projects["P2"].ai_created == {}


def test_empty_settings_have_empty_choice_dicts():
    s = Settings.empty()
    assert s.feature_import == {} and s.ai_created == {}


@pytest.mark.parametrize("text,needle", [
    ("featureImport: {createTestSet: yes-please}", "featureImport.createTestSet"),
    ("featureImport: {backgrounds: both}", "featureImport.backgrounds"),
    ("featureImport: {tagsAsLabels: 1}", "featureImport.tagsAsLabels"),
    ("featureImport: {unmatchedTags: drop}", "featureImport.unmatchedTags"),
    ("featureImport: {tagRules: x}", "featureImport.tagRules"),
    ("featureImport: {bogus: 1}", "featureImport"),
    ("aiCreated: {label: 'has space'}", "aiCreated.label"),
    ("aiCreated: {label: ''}", "aiCreated.label"),
    ("aiCreated: {label: 5}", "aiCreated.label"),
    ("aiCreated: {scope: everything}", "aiCreated.scope"),
    ("projects: {P1: {aiCreated: {scope: x}}}", "projects.P1.aiCreated.scope"),
    ("projects: {P1: {featureImport: []}}", "projects.P1.featureImport"),
])
def test_invalid_choice_settings_raise_with_file_and_key(tmp_path, text, needle):
    store = store_for(tmp_path, text)
    with pytest.raises(SettingsError) as ei:
        store.get()
    assert needle in ei.value.message
    assert ei.value.data["file"].endswith("settings.yml")


RULES_YAML = """\
featureImport:
  tagRules:
    - {tag: area, field: {component: "{value}"}}
projects:
  P1:
    fields: {test: {component: {id: customfield_1}}}
    featureImport:
      tagRules:
        - {tag: x, field: {severity: "{value}"}}
  P2:
    fields: {test: {severity: {id: customfield_2}}}
"""


def test_tag_rules_parsed_at_load_project_and_top_level(tmp_path):
    y = RULES_YAML.replace("{severity: \"{value}\"}", "{component: \"{value}\"}")
    s = store_for(tmp_path, y).get()
    assert isinstance(s.feature_import["tagRules"][0], TagRule)
    assert s.feature_import["tagRules"][0].fields == {"component": "{value}"}
    assert isinstance(s.projects["P1"].feature_import["tagRules"][0], TagRule)


def test_project_rule_unknown_field_is_settings_error_with_file_key_index(tmp_path):
    with pytest.raises(SettingsError) as ei:
        store_for(tmp_path, RULES_YAML).get()   # severity is only defined for P2
    assert "projects.P1.featureImport.tagRules[0]" in ei.value.message
    assert "severity" in ei.value.message
    assert ei.value.data["file"].endswith("settings.yml")


def test_top_level_rule_validated_against_union_of_project_fields(tmp_path):
    y = ("featureImport: {tagRules: [{tag: a, label: x}, {tag: b, field: {severity: v}}]}\n"
         "projects:\n  P1: {fields: {test: {severity: {id: c1}}}}\n  P2: {}\n")
    assert store_for(tmp_path, y).get().feature_import["tagRules"][1].fields == {"severity": "v"}
    bad = y.replace("severity: v", "nope: v")
    with pytest.raises(SettingsError) as ei:
        store_for(tmp_path, bad).get()
    assert "featureImport.tagRules[1]" in ei.value.message and "nope" in ei.value.message


@pytest.mark.parametrize("rule", ["{tag: a}", "{tag: a, match: b, label: x}",
                                  "{tag: a, skip: true, label: x}", "{tag: a, label: x, zzz: 1}"])
def test_bad_rules_fail_settings_load(tmp_path, rule):
    with pytest.raises(SettingsError) as ei:
        store_for(tmp_path, "featureImport: {tagRules: [%s]}" % rule).get()
    assert "featureImport.tagRules[0]" in ei.value.message


# --- testKeyTag (result import) ------------------------------------------------------------

def test_test_key_tag_parsed_per_project(tmp_path):
    s = store_for(tmp_path, "projects:\n  P1: {testKeyTag: issue}\n  P2: {}\n").get()
    assert s.projects["P1"].test_key_tag == "issue"
    assert s.projects["P2"].test_key_tag is None


@pytest.mark.parametrize("value", ["'@issue'", "'has space'", "5", "'a=b'", "''", "'x:y'"])
def test_invalid_test_key_tag_refused(tmp_path, value):
    store = store_for(tmp_path, "projects:\n  P1: {testKeyTag: %s}\n" % value)
    with pytest.raises(SettingsError) as ei:
        store.get()
    assert "projects.P1.testKeyTag" in ei.value.message


def test_project_dir_prefers_xray_project_dir(tmp_path, monkeypatch):
    from xray_cloud.config import project_dir
    monkeypatch.chdir(tmp_path)
    both = {"XRAY_PROJECT_DIR": "/x/xray", "CLAUDE_PROJECT_DIR": "/x/claude"}
    assert project_dir(both) == "/x/xray"
    assert project_dir({"CLAUDE_PROJECT_DIR": "/x/claude"}) == "/x/claude"
    assert project_dir({}) == os.getcwd()


def test_missing_credentials_message_is_client_neutral():
    from xray_cloud.context import NO_CREDENTIALS_MESSAGE as m
    assert "XRAY_CLIENT_ID" in m and "XRAY_CLIENT_SECRET" in m
    assert "your MCP client's server config" in m
    assert "Claude Code's plugin settings" in m


def test_no_jira_fix_is_client_neutral():
    from xray_cloud.notdone import NO_JIRA_FIX
    for name in ("JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        assert name in NO_JIRA_FIX


def test_unsubstituted_placeholders_are_ignored(monkeypatch):
    from xray_cloud.config import project_dir
    assert project_dir({"XRAY_PROJECT_DIR": "${workspacePath}", "CLAUDE_PROJECT_DIR": "/x/claude"}) == "/x/claude"
    assert project_dir({"XRAY_PROJECT_DIR": "${workspacePath}"}) == os.getcwd()
    assert project_dir({"XRAY_PROJECT_DIR": "${user_config.x}"}) == os.getcwd()
    assert project_dir({"XRAY_PROJECT_DIR": "/a/${weird}/b"}) == "/a/${weird}/b"


def test_project_dir_unknown_inside_plugin_folder(tmp_path, monkeypatch):
    from xray_cloud import config
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    monkeypatch.setattr(config, "PLUGIN_DIR", plugin)
    monkeypatch.chdir(plugin)
    assert config.project_dir_info({}) == (None, None)
    assert config.project_dir_info({"CLAUDE_PROJECT_DIR": "/w"}) == ("/w", "CLAUDE_PROJECT_DIR")
    monkeypatch.chdir(tmp_path)
    assert config.project_dir_info({}) == (str(tmp_path), "cwd")


def test_plugin_dir_is_the_plugin_folder():
    from xray_cloud import config
    assert (config.PLUGIN_DIR / ".claude-plugin" / "plugin.json").is_file()
