from __future__ import annotations

import pytest

from xray_cloud.config import FieldSpec, ProjectSettings
from xray_cloud.errors import ToolError
from xray_cloud.fields import build_jira_fields, build_run_fields


def spec(name, id, type="string", required=False, default=None, xray_name=None):
    return FieldSpec(name=name, id=id, xray_name=xray_name, type=type, required=required, default=default)


P = ProjectSettings("PROJ", {
    "test": {
        "component": spec("component", "customfield_12345", "option", True, "Backend"),
        "tags": spec("tags", "customfield_10051", "options"),
        "fixVersions": spec("fixVersions", "customfield_10052", "versions"),
        "fixVersion": spec("fixVersion", "customfield_10053", "version"),
        "owner": spec("owner", "customfield_10054", "user"),
        "points": spec("points", "customfield_10055", "number"),
        "notes": spec("notes", "customfield_10056", "string"),
        "blob": spec("blob", "customfield_10057", "raw"),
    },
    "testPlan": {
        "team": spec("team", "customfield_20001", "string", True),
        "area": spec("area", "customfield_20002", "string", True),
    },
    "testRun": {
        "env": spec("env", None, "string", xray_name="Environment"),
        "build": spec("build", "cf_9", "string"),
    },
})


def test_option_string_becomes_value_object():
    assert build_jira_fields(P, "test", {"component": "Backend"}, None) == {"customfield_12345": {"value": "Backend"}}


def test_option_dict_passes_through():
    out = build_jira_fields(P, "test", {"component": {"id": "5"}}, None)
    assert out == {"customfield_12345": {"id": "5"}}


def test_options_list():
    out = build_jira_fields(P, "test", {"tags": ["A", "B"]}, None)
    assert out["customfield_10051"] == [{"value": "A"}, {"value": "B"}]


def test_versions_names():
    assert build_jira_fields(P, "test", {"fixVersions": ["1.2"]}, None)["customfield_10052"] == [{"name": "1.2"}]
    assert build_jira_fields(P, "test", {"fixVersions": "1.2"}, None)["customfield_10052"] == [{"name": "1.2"}]


def test_version_single():
    assert build_jira_fields(P, "test", {"fixVersion": "1.2"}, None)["customfield_10053"] == {"name": "1.2"}


def test_user_account_id():
    assert build_jira_fields(P, "test", {"owner": "abc"}, None)["customfield_10054"] == {"accountId": "abc"}


def test_number_and_passthrough():
    out = build_jira_fields(P, "test", {"points": "3", "notes": "hi", "blob": {"x": 1}}, None)
    assert out["customfield_10055"] == 3 and isinstance(out["customfield_10055"], int)
    assert build_jira_fields(P, "test", {"points": "2.5"}, None)["customfield_10055"] == 2.5
    assert build_jira_fields(P, "test", {"points": 4}, None)["customfield_10055"] == 4
    assert out["customfield_10056"] == "hi" and out["customfield_10057"] == {"x": 1}


def test_default_applied_when_absent():
    assert build_jira_fields(P, "test", None, None) == {"customfield_12345": {"value": "Backend"}}


def test_missing_required_lists_all_missing():
    with pytest.raises(ToolError) as e:
        build_jira_fields(P, "testPlan", {}, None)
    assert e.value.data["missingFields"] == [
        {"name": "team", "id": "customfield_20001"},
        {"name": "area", "id": "customfield_20002"},
    ]


def test_raw_satisfies_required():
    out = build_jira_fields(P, "testPlan", None, {"customfield_20001": "x", "customfield_20002": "y"})
    assert out == {"customfield_20001": "x", "customfield_20002": "y"}


def test_unknown_friendly_name_is_error_and_lists_known_names():
    with pytest.raises(ToolError) as e:
        build_jira_fields(P, "test", {"bogus": 1}, None)
    assert "component" in e.value.data["knownFields"]


def test_absent_item_type_has_no_fields():
    assert build_jira_fields(P, "testSet", None, {"customfield_1": "v"}) == {"customfield_1": "v"}
    with pytest.raises(ToolError) as e:
        build_jira_fields(P, "testSet", {"x": 1}, None)
    assert e.value.data["knownFields"] == []


def test_raw_fields_pass_through_and_win_on_conflict():
    out = build_jira_fields(P, "test", {"component": "Backend"}, {"customfield_12345": {"value": "Other"}, "labels": ["a"]})
    assert out == {"customfield_12345": {"value": "Other"}, "labels": ["a"]}


def test_run_fields_use_xray_names():
    out = build_run_fields(P, {"env": "qa", "build": "42"}, {"Extra": "z"})
    assert out == [{"name": "Environment", "value": "qa"}, {"id": "cf_9", "value": "42"},
                   {"name": "Extra", "value": "z"}]


def test_run_unknown_name():
    with pytest.raises(ToolError) as e:
        build_run_fields(P, {"nope": 1}, None)
    assert e.value.data["knownFields"] == ["env", "build"]
