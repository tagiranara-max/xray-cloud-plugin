"""Shared helpers for the Gherkin tests (round-trip wrappers and comparison keys)."""
from __future__ import annotations

from pathlib import Path

from xray_cloud.gherkin.parse import ParsedBackground, ParsedFeature, ParsedScenario, ParsedStep
from xray_cloud.gherkin.serialize import background_as_feature, scenario_as_feature

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "gherkin"


def wrap_as_feature(s: ParsedScenario, language: str = "en") -> str:
    return scenario_as_feature(s, language)


def wrap_background(b: ParsedBackground, language: str = "en") -> str:
    return background_as_feature(b, language)


def step_key(st: ParsedStep):
    ds = st.doc_string
    return (
        st.keyword.strip(),
        st.text,
        st.data_table,
        (ds.content, ds.media_type) if ds is not None else None,
    )


def steps_of(steps) -> list:
    return [step_key(st) for st in steps]


def steps_and_examples(s: ParsedScenario):
    return (
        steps_of(s.steps),
        [(e.name, e.description, e.tags, e.header, e.rows) for e in s.examples],
    )


def all_backgrounds(feat: ParsedFeature) -> list:
    return list(feat.backgrounds)
