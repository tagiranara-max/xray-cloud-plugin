"""Regression fixtures for past feature-parsing failures (data-table rows and multi-line
text skipped) plus every other Gherkin construct the import must keep. Each case is checked
on the parsed AST, on the generated Xray definition, and on the definition re-parsed."""
from __future__ import annotations

import pytest

from gherkin_helpers import FIXTURES, steps_and_examples, steps_of, wrap_as_feature, wrap_background
from xray_cloud.gherkin.parse import DocString, GherkinError, normalise_text, parse_feature
from xray_cloud.gherkin.serialize import background_definition, scenario_definition

REG = FIXTURES / "regression"


def load(name):
    path = REG / name
    return parse_feature(path.read_bytes().decode("utf-8"), str(path))


def scenario(feat, name):
    matches = [s for s in feat.scenarios if s.name == name]
    assert len(matches) == 1, [s.name for s in feat.scenarios]
    return matches[0]


def assert_round_trips(feat):
    for s in feat.scenarios:
        again = parse_feature(wrap_as_feature(s, feat.language), "rt")
        assert steps_and_examples(again.scenarios[0]) == steps_and_examples(s)
    for b in feat.backgrounds:
        again = parse_feature(wrap_background(b, feat.language), "bg")
        assert steps_of(again.backgrounds[0].steps) == steps_of(b.steps)


@pytest.mark.parametrize("name", sorted(p.name for p in REG.glob("*.feature")))
def test_every_regression_file_round_trips(name):
    assert_round_trips(load(name))


# -- regression 1: data-table rows skipped -------------------------------------------------

def test_datatable_keeps_every_row_including_header():
    feat = load("datatables.feature")
    s = scenario(feat, "Tables on every step keyword")
    assert [st.keyword.strip() for st in s.steps] == ["Given", "When", "Then", "And", "But", "*"]
    assert s.steps[0].data_table == [["g1", "g2"], ["ga", "gb"]]
    assert s.steps[5].data_table == [["s1", "s2"], ["sa", "sb"]]
    assert all(st.data_table and len(st.data_table) == 2 for st in s.steps)
    d = scenario_definition(s)
    assert d.splitlines() == [
        "Given a given table:",
        "  | g1 | g2 |",
        "  | ga | gb |",
        "When a when table:",
        "  | w1 |",
        "  | wa |",
        "Then a then table:",
        "  | t1 | t2 | t3 |",
        "  | ta | tb | tc |",
        "And an and table:",
        "  | a1 |",
        "  | aa |",
        "But a but table:",
        "  | b1 |",
        "  | bb |",
        "* a star table:",
        "  | s1 | s2 |",
        "  | sa | sb |",
    ]
    # sample.feature from the old server: the header row was the one that went missing
    sample = load("sample.feature")
    st = scenario(sample, "Successful login").steps[0]
    assert st.data_table == [["username", "password"], ["admin", "secret"]]
    assert "| username | password |" in scenario_definition(scenario(sample, "Successful login"))


def test_datatable_cells_with_escapes_survive():
    feat = load("datatables.feature")
    s = scenario(feat, "Cells with escapes, empty cells and unicode")
    assert s.steps[0].data_table == [
        ["pipe", "newline", "backslash", "empty", "unicode"],
        ["a | b", "line1\nline2", "c:\\dir", "", "Zürich ✓ 日本"],
        ["|", "\n", "\\", "", "ß"],
        ["x\\|y", "end\\", "", '""', "🙂"],
    ]
    d = scenario_definition(s)
    assert "  | a \\| b | line1\\nline2 | c:\\\\dir |  | Zürich ✓ 日本 |" in d
    assert "  | \\| | \\n | \\\\ |  | ß |" in d
    assert "  | x\\\\\\|y | end\\\\ |  | \"\" | 🙂 |" in d
    again = parse_feature(wrap_as_feature(s), "rt").scenarios[0]
    assert again.steps[0].data_table == s.steps[0].data_table


def test_datatables_in_background_and_outline_steps():
    feat = load("datatables.feature")
    (bg,) = feat.backgrounds
    assert bg.scope == "feature"
    assert bg.steps[0].data_table == [["name", "balance"], ["alice", "10"], ["bob", "20"]]
    assert background_definition(bg).splitlines() == [
        "Given the following accounts exist:",
        "  | name | balance |",
        "  | alice | 10 |",
        "  | bob | 20 |",
    ]
    o = scenario(feat, "Tables inside outline steps")
    assert o.is_outline
    assert o.steps[0].data_table == [["item", "qty"], ["<item>", "<qty>"]]
    assert o.examples[0].header == ["user", "item", "qty", "total"]
    assert o.examples[0].rows == [["alice", "apple", "2", "4"], ["bob", "pear", "1", "3"]]
    d = scenario_definition(o)
    assert "Background" not in d and "accounts" not in d
    assert d.splitlines()[:3] == ["Given user <user> has items:", "  | item | qty |",
                                  "  | <item> | <qty> |"]
    assert "  | alice | apple | 2 | 4 |" in d


# -- regression 2: multi-line text skipped -------------------------------------------------

def test_docstring_keeps_every_line():
    feat = load("docstrings.feature")
    s = scenario(feat, "Multi-line doc string with blank lines and indentation")
    ds = s.steps[0].doc_string
    assert ds == DocString(
        content="first line\n\nthird line after a blank line\n  indented by two\n"
                "    indented by four\n\n\nafter two blank lines",
        delimiter='"""', media_type=None)
    d = scenario_definition(s)
    assert ('  """\n  first line\n\n  third line after a blank line\n    indented by two\n'
            '      indented by four\n\n\n  after two blank lines\n  """') in d
    again = parse_feature(wrap_as_feature(s), "rt").scenarios[0]
    assert again.steps[0].doc_string.content == ds.content


def test_docstring_backtick_delimiter_and_media_type():
    feat = load("docstrings.feature")
    s = scenario(feat, "Multi-line doc string with blank lines and indentation")
    assert s.steps[1].doc_string == DocString(
        content='{\n  "key": "value",\n  "list": [1, 2]\n}', delimiter="```", media_type="json")
    assert s.steps[2].doc_string.media_type == "xml"
    assert s.steps[2].doc_string.delimiter == '"""'
    d = scenario_definition(s)
    assert '  ```json\n  {\n    "key": "value",' in d
    assert '  """xml\n  <a>\n    <b/>\n  </a>\n  """' in d


def test_docstring_containing_triple_quotes():
    feat = load("docstrings.feature")
    s = scenario(feat, "Doc string containing delimiters")
    quotes, ticks, quotes_in_ticks, both = (st.doc_string for st in s.steps)
    assert quotes.content == 'before\n"""\nafter'
    assert ticks.content == "before\n```\nafter"
    assert quotes_in_ticks.content == '"""\nnot a closing delimiter\n"""'
    assert both.content == '"""\n```'
    d = scenario_definition(s)
    # content contains the original delimiter -> the other one is chosen
    assert '  ```\n  before\n  """\n  after\n  ```' in d
    assert '  """\n  before\n  ```\n  after\n  """' in d
    # content contains both -> original delimiter, escaped
    assert '  """\n  \\"\\"\\"\n  ```\n  """' in d
    again = parse_feature(wrap_as_feature(s), "rt").scenarios[0]
    assert [st.doc_string.content for st in again.steps] == [
        quotes.content, ticks.content, quotes_in_ticks.content, both.content]


def test_multiline_descriptions_kept():
    feat = load("descriptions.feature")
    assert feat.description == ("This feature description spans\nseveral lines.\n\n"
                                "  One line is indented further.")
    s = feat.scenarios[0]
    assert s.description == ("The scenario description\nalso spans several lines.\n\n"
                             "It even has a blank line.")
    assert [st.text for st in s.steps] == ["something", "something else"]
    assert "description" not in scenario_definition(s)


# -- other constructs ----------------------------------------------------------------------

def test_rule_scenarios_not_dropped_and_rule_background_scoped():
    feat = load("rules.feature")
    assert feat.tags == ["@feature"]
    names = [s.name for s in feat.scenarios]
    assert names == ["Top-level scenario", "Inside first rule", "Outline inside first rule",
                     "Inside second rule"]
    top, in1, out1, in2 = feat.scenarios
    assert top.rule is None and in1.rule == "First rule" and in2.rule == "Second rule without background"
    assert in1.rule_tags == ["@rule-tag"] and in1.tags == ["@scenario-tag"]
    assert top.rule_tags == [] and in2.rule_tags == []
    fbg, rbg = feat.backgrounds
    assert (fbg.scope, fbg.name) == ("feature", "Feature background")
    assert (rbg.scope, rbg.name) == ("rule", "Rule background")
    assert rbg.steps[0].data_table == [["k", "v"], ["1", "2"]]
    assert top.backgrounds == [fbg]
    assert in1.backgrounds == [fbg, rbg] and out1.backgrounds == [fbg, rbg]
    assert in2.backgrounds == [fbg]
    assert out1.is_outline and out1.examples[0].rows == [["1"]]
    assert top.line == 7 and in1.line == 20
    assert "rule background" not in scenario_definition(in1)
    assert background_definition(rbg).splitlines() == [
        "Given the rule background", "  | k | v |", "  | 1 | 2 |"]


def test_examples_names_descriptions_tags_kept():
    feat = load("examples.feature")
    o, t = feat.scenarios
    assert o.tags == ["@outline-tag"] and o.is_outline
    e1, e2, e3 = o.examples
    assert (e1.name, e1.tags) == ("First block", ["@smoke", "@fast"])
    assert e1.description == "This block has a description\nover two lines."
    assert (e1.header, e1.rows) == (["a", "b"], [["1", "2"]])
    assert (e2.name, e2.tags, e2.description) == ("Second block", ["@slow"], "")
    assert e2.rows == [["3", "4"], ["5", "6"]]
    assert (e3.name, e3.tags, e3.rows) == ("", [], [["7", "8"]])
    assert scenario_definition(o).splitlines() == [
        "Given <a> and <b>",
        "",
        "@smoke @fast",
        "Examples: First block",
        "  This block has a description",
        "  over two lines.",
        "",
        "  | a | b |",
        "  | 1 | 2 |",
        "",
        "@slow",
        "Examples: Second block",
        "  | a | b |",
        "  | 3 | 4 |",
        "  | 5 | 6 |",
        "",
        "Examples:",
        "  | a | b |",
        "  | 7 | 8 |",
    ]
    assert t.is_outline and t.keyword == "Scenario Template"
    assert t.examples[0].keyword == "Scenarios" and t.examples[0].name == "Alt keyword"
    assert "Scenarios: Alt keyword" in scenario_definition(t)


def test_bom_crlf_tabs_non_ascii_preserved():
    raw = (REG / "bom_crlf.feature").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw  # fixture bytes intact in git
    feat = load("bom_crlf.feature")
    assert feat.name == "BOM, CRLF, tabs and non-ASCII"
    s = feat.scenarios[0]
    assert s.name == "Grüße\tmit Tab"
    assert s.steps[0].text == "a step with\ta tab and ünïcödé ✓"
    assert s.steps[0].data_table == [["col\tA", "日本"], ["x\ty", "ü"]]
    assert s.steps[1].doc_string.content == "\tleading tab\némoji 🙂"
    d = scenario_definition(s)
    assert "\r" not in d and "\ufeff" not in d
    assert "  | col\tA | 日本 |" in d and "  \tleading tab" in d


def test_lone_cr_line_endings_normalised():
    text = "Feature: Old Mac\r\r  Scenario: s\r    Given a\r      | x | y |\r    Then b\r"
    s = parse_feature(text, "mac").scenarios[0]
    assert [st.text for st in s.steps] == ["a", "b"]
    assert s.steps[0].data_table == [["x", "y"]]
    assert normalise_text("\ufeffa\r\nb\rc\n") == "a\nb\nc\n"


def test_language_header_de():
    feat = load("language_de.feature")
    assert feat.language == "de"
    assert feat.keyword == "Funktionalität"
    assert feat.backgrounds[0].steps[0].keyword == "Angenommen "
    sz, gr = feat.scenarios
    assert sz.keyword == "Szenario" and not sz.is_outline
    assert [st.keyword.strip() for st in sz.steps] == ["Angenommen", "Wenn", "Dann", "Und"]
    assert sz.steps[0].data_table == [["Spalte", "Wert"], ["ä", "ö"]]
    assert gr.is_outline and gr.examples[0].keyword == "Beispiele"
    # Xray needs the English keyword to treat the test as an outline (verified live)
    assert "Examples: Werte" in scenario_definition(gr)
    assert "Beispiele" not in scenario_definition(gr)
    assert "Beispiele: Werte" in scenario_definition(gr, keep_examples_keyword=True)
    wrapped = wrap_as_feature(gr, feat.language)
    assert wrapped.startswith("# language: de\n")
    assert steps_and_examples(parse_feature(wrapped, "rt").scenarios[0]) == steps_and_examples(gr)


def test_scenario_with_examples_but_scenario_keyword_is_outline():
    feat = parse_feature(
        "Feature: f\n  Scenario: s\n    Given <x>\n    Examples:\n      | x |\n      | 1 |\n", "t")
    assert feat.scenarios[0].is_outline


def test_empty_and_comment_only_inputs_have_no_scenarios():
    for text in ("", "\ufeff", "# just a comment\n", "\n\n"):
        feat = parse_feature(text, "t")
        assert feat.scenarios == [] and feat.backgrounds == [] and feat.language == "en"


def test_parse_error_location_and_source():
    with pytest.raises(GherkinError) as e:
        parse_feature("Feature: f\n  Scenario: s\n    Given x\n      | a | b |\n      | c |\n",
                      "my.feature")
    assert e.value.data["source"] == "my.feature"
    assert (e.value.data["line"], e.value.data["column"]) == (5, 7)
    assert "my.feature" in e.value.message
    assert e.value.to_dict()["error"] == e.value.message
