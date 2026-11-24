"""Cucumber's own conformance corpus (testdata/good and testdata/bad of cucumber/gherkin,
at the tag matching the vendored parser): every good file converts and round-trips through
our serialiser; every bad file fails with a located GherkinError."""
from __future__ import annotations

import pytest

from gherkin_helpers import (
    FIXTURES, all_backgrounds, steps_and_examples, steps_of, wrap_as_feature, wrap_background,
)
from xray_cloud.errors import ToolError
from xray_cloud.gherkin.parse import GherkinError, parse_feature

GOOD_FILES = sorted((FIXTURES / "good").glob("*.feature"))
BAD_FILES = sorted((FIXTURES / "bad").glob("*.feature"))


def test_corpus_present():
    assert len(GOOD_FILES) == 46
    assert len(BAD_FILES) == 11


@pytest.mark.parametrize("path", GOOD_FILES, ids=lambda p: p.name)
def test_every_good_file_round_trips(path):
    feat = parse_feature(path.read_text(encoding="utf-8"), str(path))
    for s in feat.scenarios:
        again = parse_feature(wrap_as_feature(s, feat.language), "roundtrip")
        assert len(again.scenarios) == 1
        assert again.scenarios[0].is_outline == s.is_outline
        assert steps_and_examples(again.scenarios[0]) == steps_and_examples(s)
    for b in all_backgrounds(feat):
        again = parse_feature(wrap_background(b, feat.language), "bg")
        assert steps_of(again.backgrounds[0].steps) == steps_of(b.steps)


@pytest.mark.parametrize("path", BAD_FILES, ids=lambda p: p.name)
def test_every_bad_file_raises_with_location(path):
    with pytest.raises(GherkinError) as e:
        parse_feature(path.read_text(encoding="utf-8"), str(path))
    err = e.value
    assert isinstance(err, ToolError)
    assert err.data["source"] == str(path)
    assert err.data["line"] >= 1
    assert err.data["column"] >= 1
    assert err.data["errors"] and all(x["message"] for x in err.data["errors"])
    assert "line %d" % err.data["line"] in err.message


def test_multiple_parser_errors_are_all_reported_first_location_wins():
    path = FIXTURES / "bad" / "multiple_parser_errors.feature"
    with pytest.raises(GherkinError) as e:
        parse_feature(path.read_text(encoding="utf-8"), str(path))
    errs = e.value.data["errors"]
    assert len(errs) >= 2
    assert (e.value.data["line"], e.value.data["column"]) == (errs[0]["line"], errs[0]["column"])
