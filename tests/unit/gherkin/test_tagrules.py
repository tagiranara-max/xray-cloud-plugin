from __future__ import annotations

import pytest

from xray_cloud.config import SettingsError
from xray_cloud.errors import ToolError
from xray_cloud.gherkin.tagrules import (
    TagOutcome, apply_rules, merge_outcomes, parse_rules)

FIELDS = {"component", "area"}


def rules(*raw, fields=FIELDS):
    return parse_rules(list(raw), set(fields), "featureImport.tagRules")


def run(tags, *raw, labels=True, unmatched="keep"):
    return apply_rules(tags, rules(*raw), labels, unmatched)


# --- matching -------------------------------------------------------------

@pytest.mark.parametrize("tag", ["suite=smoke", "suite:smoke", "SUITE=smoke", "Suite:smoke"])
def test_key_form_matches_both_separators_case_insensitive_key(tag):
    o = run([tag], {"tag": "suite", "label": "{value}"})
    assert o.labels == ["smoke"] and o.consumed == [tag] and o.unmatched == []


def test_value_case_is_preserved():
    assert run(["suite=Smoke"], {"tag": "suite", "label": "{value}"}).labels == ["Smoke"]


@pytest.mark.parametrize("tag", ["suite", "suite=", "suite:", "suite=   ", "other=x"])
def test_key_form_does_not_match_without_separator_or_value(tag):
    o = run([tag], {"tag": "suite", "label": "{value}"}, unmatched="skip")
    assert o.labels == [] and o.consumed == [] and o.unmatched == [tag]


def test_key_form_without_separator_falls_to_later_rule():
    o = run(["suite"], {"tag": "suite", "label": "x-{value}"}, {"match": "suite", "label": "plain"})
    assert o.labels == ["plain"]


def test_list_of_keys():
    r = {"tag": ["area", "areas"], "label": "{key}-{value}"}
    assert run(["area=a", "areas=b"], r).labels == ["area-a", "areas-b"]


def test_split_trims_and_drops_empties():
    o = run(["areas= a , b,, c ,"], {"tag": "areas", "split": ",", "label": "{value}"})
    assert o.labels == ["a", "b", "c"]


def test_split_with_only_empty_values_does_not_match():
    o = run(["areas=, ,"], {"tag": "areas", "split": ",", "label": "{value}"}, unmatched="skip")
    assert o.consumed == [] and o.unmatched == ["areas=, ,"]


def test_regex_full_match_with_groups_and_tag_placeholder():
    r = {"match": r"(wip|draft)-(\d+)", "label": "state-$1", "testSet": "{tag}/$2"}
    o = run(["wip-12"], r)
    assert o.labels == ["state-wip"] and o.test_sets == ["wip-12/12"]


def test_regex_is_full_match_not_search():
    o = run(["xwip"], {"match": "wip", "label": "a"}, unmatched="skip")
    assert o.consumed == [] and o.unmatched == ["xwip"]


def test_first_matching_rule_wins():
    o = run(["suite=x"], {"tag": "suite", "label": "first"}, {"match": "suite=x", "label": "second"})
    assert o.labels == ["first"]


def test_multi_action_rule():
    o = run(["suite=checkout"], {"tag": "suite", "label": "{value}", "testSet": "Suite {value}",
                            "field": {"component": "{value}", "area": "{key}"}})
    assert o.labels == ["checkout"] and o.test_sets == ["Suite checkout"]
    assert o.fields == {"component": "checkout", "area": "suite"}


def test_label_and_testset_lists_and_dedup():
    o = run(["a=x", "b=x"], {"tag": ["a", "b"], "label": ["{value}", "all"],
                             "testSet": ["S", "T-{value}"]})
    assert o.labels == ["x", "all"] and o.test_sets == ["S", "T-x"]


def test_split_repeats_actions_per_value():
    o = run(["areas=a,b"], {"tag": "areas", "split": ",", "label": "area-{value}",
                            "testSet": "{value}"})
    assert o.labels == ["area-a", "area-b"] and o.test_sets == ["a", "b"]


def test_skip_consumes_without_output():
    o = run(["wip", "keep"], {"match": "wip", "skip": True})
    assert o.consumed == ["wip"] and o.unmatched == ["keep"] and o.labels == ["keep"]


def test_unmatched_keep_vs_skip():
    # an uncovered key:value tag is never kept as a label (the import asks about its key)
    keep = run(["a", "b=c", "d:e", "f:"], {"tag": "x", "label": "l"}, unmatched="keep")
    assert keep.labels == ["a", "f:"] and keep.unmatched == ["a", "b=c", "d:e", "f:"]
    assert keep.consumed == []
    skip = run(["a", "b=c"], {"tag": "x", "label": "l"}, unmatched="skip")
    assert skip.labels == [] and skip.unmatched == ["a", "b=c"]


def test_tags_as_labels_false_drops_unmatched_plain_tags_only():
    o = run(["suite=p", "plain"], {"tag": "suite", "label": "{value}", "testSet": "{value}",
                                    "field": {"component": "{value}"}}, labels=False)
    assert o.labels == ["p"] and o.test_sets == ["p"] and o.fields == {"component": "p"}
    assert o.consumed == ["suite=p"] and o.unmatched == ["plain"]


def test_kv_key_covers_both_separators_and_needs_a_value():
    from xray_cloud.gherkin.tagrules import kv_key
    assert kv_key("Suite:smoke") == "suite" and kv_key("suite=regression") == "suite"
    assert kv_key("suite:") is None and kv_key("plain") is None


def test_rule_field_accepts_a_custom_field_id():
    rules = parse_rules([{"tag": "x", "field": {"customfield_77": "{value}"}}], set(), "r")
    assert apply_rules(["x:1"], rules, True).fields == {"customfield_77": "1"}
    with pytest.raises(ToolError):
        parse_rules([{"tag": "x", "field": {"Nope": "{value}"}}], set(), "r")


def test_no_rules_behaves_per_flags():
    assert apply_rules(["a"], [], True, "keep").labels == ["a"]
    assert apply_rules(["a"], [], True, "skip").labels == []
    assert apply_rules(["a"], [], False, "keep").labels == []


# --- problems ------------------------------------------------------------

def test_label_with_whitespace_is_a_problem_naming_tag_rule_and_result():
    o = run(["suite=a b"], {"tag": "x", "label": "n"}, {"tag": "suite", "label": "{value}"})
    assert o.labels == []
    assert len(o.problems) == 1
    p = o.problems[0]
    assert "suite=a b" in p and "rule 1" in p and "'a b'" in p


def test_empty_label_result_is_a_problem():
    o = run(["wip"], {"match": "(x)?wip", "label": "$1"})
    assert o.labels == [] and len(o.problems) == 1


def test_unmatched_kept_tag_with_whitespace_is_a_problem():
    o = run(["a b"], {"tag": "x", "label": "l"})
    assert o.labels == [] and "a b" in o.problems[0]


def test_empty_test_set_name_is_a_problem():
    o = run(["wip"], {"match": "(x)?wip", "testSet": " $1 "})
    assert o.test_sets == [] and len(o.problems) == 1


def test_conflicting_field_values_in_one_outcome_is_a_problem():
    o = run(["areas=a,b"], {"tag": "areas", "split": ",", "field": {"area": "{value}"}})
    assert o.fields == {"area": "a"}
    assert "area" in o.problems[0] and "'a'" in o.problems[0] and "'b'" in o.problems[0]


def test_same_field_value_twice_is_fine():
    o = run(["a=x", "b=x"], {"tag": ["a", "b"], "field": {"area": "{value}"}})
    assert o.fields == {"area": "x"} and o.problems == []


def test_unmatched_group_renders_empty():
    o = run(["wip"], {"match": "(x)?wip", "label": "a$1b"})
    assert o.labels == ["ab"]


# --- merge ----------------------------------------------------------------

def test_merge_unions_preserving_first_seen_order():
    a = TagOutcome(labels=["x", "y"], test_sets=["S"], consumed=["t1"], unmatched=["u"])
    b = TagOutcome(labels=["y", "z"], test_sets=["S", "T"], consumed=["t2"], unmatched=["u", "v"])
    m = merge_outcomes(a, b)
    assert m.labels == ["x", "y", "z"] and m.test_sets == ["S", "T"]
    assert m.consumed == ["t1", "t2"] and m.unmatched == ["u", "v"]


def test_merge_field_conflict_names_field_and_both_values():
    a = run(["a=1"], {"tag": "a", "field": {"area": "{value}"}})
    b = run(["b=2"], {"tag": "b", "field": {"area": "{value}"}})
    m = merge_outcomes(a, b)
    assert m.fields == {"area": "1"}
    assert len(m.problems) == 1
    p = m.problems[0]
    assert "area" in p and "'1'" in p and "'2'" in p and "a=1" in p and "b=2" in p


def test_merge_same_field_value_ok_and_problems_carry_over():
    a = TagOutcome(fields={"area": "1"}, problems=["boom"])
    b = TagOutcome(fields={"area": "1", "component": "c"})
    m = merge_outcomes(a, b)
    assert m.fields == {"area": "1", "component": "c"} and m.problems == ["boom"]


# --- parse validation -----------------------------------------------------

@pytest.mark.parametrize("raw,needle", [
    ({"tag": "a", "match": "b", "label": "x"}, "exactly one"),
    ({"label": "x"}, "exactly one"),
    ({"tag": "a"}, "no action"),
    ({"tag": "a", "skip": True, "label": "x"}, "skip"),
    ({"tag": "a", "skip": True, "testSet": "x"}, "skip"),
    ({"tag": "a", "skip": True, "field": {"area": "x"}}, "skip"),
    ({"tag": "a", "label": "x", "bogus": 1}, "unknown key"),
    ({"tag": "a", "field": {"nope": "{value}"}}, "unknown field name"),
    ({"tag": "a", "label": "$1"}, "regex form"),
    ({"match": "(a)", "label": "$2"}, "group"),
    ({"match": "(", "label": "x"}, "regular expression"),
    ({"match": "a", "label": "{key}"}, "key form"),
    ({"match": "a", "label": "{value}"}, "key form"),
    ({"tag": "a", "label": "{bogus}"}, "placeholder"),
    ({"match": "a", "split": ",", "label": "x"}, "split"),
    ({"tag": "a", "split": "", "label": "x"}, "split"),
    ({"tag": "a=b", "label": "x"}, "keys"),
    ({"tag": "a", "label": 5}, "label"),
    ({"tag": "a", "skip": "yes"}, "skip"),
    ("nope", "mapping"),
])
def test_invalid_rules_are_refused_with_rule_index(raw, needle):
    with pytest.raises(ToolError) as ei:
        parse_rules([{"tag": "ok", "label": "ok"}, raw], FIELDS, "featureImport.tagRules")
    assert "featureImport.tagRules[1]" in ei.value.message
    assert needle in ei.value.message


def test_rules_must_be_a_list():
    with pytest.raises(ToolError):
        parse_rules({"tag": "a"}, FIELDS, "x")


def test_skip_false_with_action_is_fine():
    assert rules({"tag": "a", "label": "x", "skip": False})[0].skip is False


def test_parsed_rule_shape():
    r = rules({"tag": ["Suite", "area"], "split": ",", "label": "{value}"},
              {"match": "wip", "skip": True})
    assert r[0].keys == ["suite", "area"] and r[0].regex is None and r[0].split == ","
    assert r[0].labels == ["{value}"] and r[0].test_sets == [] and r[0].fields == {}
    assert r[1].keys is None and r[1].regex.pattern == "wip" and r[1].skip is True


# --- value maps and system fields (round 3) -------------------------------

def test_rule_field_accepts_system_field_ids():
    o = run(["prio=high", "comp=ui"], {"tag": "prio", "field": {"priority": "{value}"}},
            {"tag": "comp", "field": {"components": "{value}"}})
    assert o.fields == {"priority": "high", "components": "ui"}


def test_system_field_ids_match_the_required_field_shapes():
    from xray_cloud.gherkin.tagrules import SYSTEM_FIELD_IDS
    from xray_cloud.requiredfields import _SYSTEM_SHAPES
    assert SYSTEM_FIELD_IDS == frozenset(_SYSTEM_SHAPES)


def test_value_map_gives_the_mapped_value_and_keeps_the_tag_value():
    rule = {"tag": "team", "field": {"area": "{value}"}, "values": {"checkout": "id-7"}}
    o = run(["team=Checkout"], rule)
    assert o.fields == {"area": "id-7"} and o.raw == {"area": "Checkout"}
    assert o.mapped == {"area": "rule"} and o.rules == {"area": 0}
    o = run(["team=other"], rule)
    assert o.fields == {"area": "other"} and o.mapped == {}
    merged = merge_outcomes(TagOutcome(), run(["team=checkout"], rule))
    assert merged.fields == {"area": "id-7"} and merged.mapped == {"area": "rule"}
    assert merged.raw == {"area": "checkout"} and merged.rules == {"area": 0}


@pytest.mark.parametrize("raw,needle", [
    ({"tag": "team", "label": "x", "values": {"a": "b"}}, "only goes with `field`"),
    ({"tag": "team", "field": {"area": "{value}"}, "values": ["a"]}, "must be a mapping"),
    ({"tag": "team", "field": {"area": "{value}"}, "values": {"a": ""}}, "non-empty"),
    ({"tag": "team", "field": {"area": "{value}"}, "values": {"a": 3}}, "non-empty"),
])
def test_invalid_value_maps_are_refused(raw, needle):
    with pytest.raises(ToolError) as e:
        rules(raw)
    assert needle in e.value.message
