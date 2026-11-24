from __future__ import annotations

from xray_cloud.gherkin.parse import parse_feature
from xray_cloud.gherkin.tagrules import parse_rules
from xray_cloud.gherkin.tagsummary import summarize_tags, tag_values

TEXT = """@suite=smoke @wip @P-9
Feature: F
  @area:api,ui @suite=smoke
  Scenario: A
    Given a

  @Rule1
  Rule: R
    @suite:full @TEST_P-5 @wip
    Scenario: B
      Given b
"""


def feats(*texts):
    return [parse_feature(t, "f%d.feature" % i) for i, t in enumerate(texts)]


def rules(*raw):
    return parse_rules(list(raw), {"component"}, "t")


def test_groups_key_values_separators_lists_where_and_scenarios():
    s = summarize_tags(feats(TEXT), [], set())
    suite = s["keyValue"]["suite"]
    assert suite["values"] == {"smoke": 2, "full": 1}
    assert suite["separators"] == ["=", ":"] and suite["commaLists"] is False
    assert suite["where"] == {"feature": 1, "rule": 0, "scenario": 2}
    assert suite["scenarios"] == 2          # A and B (feature tag inherited)
    area = s["keyValue"]["area"]
    assert area["values"] == {"api": 1, "ui": 1} and area["commaLists"] is True
    assert area["separators"] == [":"] and area["scenarios"] == 1


def test_plain_and_issue_keys():
    s = summarize_tags(feats(TEXT), [], set())
    assert s["plain"]["wip"] == {"count": 2, "where": {"feature": 1, "rule": 0, "scenario": 1}}
    assert s["plain"]["Rule1"]["where"] == {"feature": 0, "rule": 1, "scenario": 0}
    assert s["issueKeys"] == ["P-9", "TEST_P-5"]
    assert "P-9" not in s["plain"]


def test_handled_vs_unmatched_against_rules_and_issue_keys_never_unmatched():
    s = summarize_tags(feats(TEXT), rules({"tag": "suite", "label": "{value}"}), set())
    assert s["handledByRules"] == ["suite=smoke", "suite:full"]
    assert s["unmatched"] == ["wip", "area:api,ui", "Rule1"]
    assert "P-9" not in s["unmatched"]
    none = summarize_tags(feats(TEXT), [], set())
    assert none["handledByRules"] == [] and "suite=smoke" in none["unmatched"]


def test_values_matching_existing_test_sets():
    s = summarize_tags(feats(TEXT), [], {"api", "other"})
    assert s["valuesMatchingTestSets"] == {"area": ["api"]}
    assert summarize_tags(feats(TEXT), [], set())["valuesMatchingTestSets"] == {}


def test_tag_values_lists_distinct_candidates():
    assert tag_values(feats(TEXT)) == ["smoke", "api", "ui", "full"]


def test_aggregates_over_features():
    s = summarize_tags(feats("@a=1\nFeature: X\n  Scenario: S\n    Given a\n",
                             "@a=1\nFeature: Y\n  Scenario: S\n    Given a\n"), [], set())
    assert s["keyValue"]["a"]["values"] == {"1": 2} and s["keyValue"]["a"]["scenarios"] == 2
