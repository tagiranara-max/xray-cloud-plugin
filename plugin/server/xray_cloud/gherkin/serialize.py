"""Serialise parsed Gherkin back to text: the definition stored in Xray for a Cucumber test
(steps, plus all Examples blocks for outlines) and for a Pre-Condition (Background steps).

Re-parsing the output yields the same step keywords, texts, data tables, doc strings and
examples (verified against Cucumber's conformance corpus)."""
from __future__ import annotations

from typing import Iterable, List

from .._vendor_path import import_vendored

import_vendored("gherkin")

from gherkin.dialect import Dialect  # noqa: E402

from .parse import DocString, ParsedBackground, ParsedExamples, ParsedScenario, ParsedStep

INDENT = "  "
_DELIMITERS = ('"""', "```")


def escape_cell(value: str) -> str:
    """Escape a table cell: backslash, pipe and newline (the only escapes Gherkin unescapes)."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def table_lines(rows: Iterable[List[str]], indent: str = INDENT) -> List[str]:
    out = []
    for row in rows:
        out.append(indent + ("| " + " | ".join(escape_cell(c) for c in row) + " |" if row else "|"))
    return out


def _escaped(delimiter: str) -> str:
    return "".join("\\" + ch for ch in delimiter)


def choose_delimiter(doc: DocString) -> str:
    """The original delimiter unless the content contains it (or its escaped form); then the
    other one if that is clean. If both occur, the original (content is escaped)."""
    original = doc.delimiter if doc.delimiter in _DELIMITERS else '"""'
    other = _DELIMITERS[1] if original == _DELIMITERS[0] else _DELIMITERS[0]
    for candidate in (original, other):
        if candidate not in doc.content and _escaped(candidate) not in doc.content:
            return candidate
    return original


def doc_string_lines(doc: DocString, indent: str = INDENT) -> List[str]:
    delimiter = choose_delimiter(doc)
    content = doc.content.replace(delimiter, _escaped(delimiter))
    out = [indent + delimiter + (doc.media_type or "")]
    out.extend(indent + line if line else "" for line in content.split("\n"))
    out.append(indent + delimiter)
    return out


def step_lines(step: ParsedStep) -> List[str]:
    out = [step.keyword + step.text]
    if step.data_table is not None:
        out.extend(table_lines(step.data_table))
    if step.doc_string is not None:
        out.extend(doc_string_lines(step.doc_string))
    return out


def steps_text(steps: Iterable[ParsedStep]) -> str:
    lines: List[str] = []
    for st in steps:
        lines.extend(step_lines(st))
    return "\n".join(lines)


# Xray recognises an outline (scenarioType scenario_outline) only by an English Examples
# keyword in the stored definition; its own import of a "# language: de" file stores
# "Examples:" too (verified live).
XRAY_EXAMPLES_KEYWORDS = ("Examples", "Scenarios")


def examples_lines(ex: ParsedExamples, keep_keyword: bool = False) -> List[str]:
    out: List[str] = []
    if ex.tags:
        out.append(" ".join(ex.tags))
    keyword = ex.keyword or "Examples"
    if not keep_keyword and keyword not in XRAY_EXAMPLES_KEYWORDS:
        keyword = "Examples"
    out.append(keyword + ":" + (" " + ex.name if ex.name else ""))
    if ex.description:
        out.extend(INDENT + line if line else "" for line in ex.description.split("\n"))
        if ex.header or ex.rows:
            out.append("")
    if ex.header or ex.rows:
        out.extend(table_lines([ex.header] + ex.rows if ex.header else ex.rows))
    return out


def scenario_definition(s: ParsedScenario, keep_examples_keyword: bool = False) -> str:
    """Gherkin stored in Xray for a Cucumber test: the steps, then (outlines) every Examples
    block with its tags, name and description. Backgrounds are not included. A non-English
    Examples keyword (e.g. "Beispiele") is written as "Examples" unless keep_examples_keyword,
    because Xray only recognises the English keywords."""
    lines = [steps_text(s.steps)] if s.steps else []
    for ex in s.examples:
        lines.append("\n".join(examples_lines(ex, keep_examples_keyword)))
    return "\n\n".join(lines)


def background_definition(b: ParsedBackground) -> str:
    """Gherkin stored in Xray for a Pre-Condition: the Background's steps."""
    return steps_text(b.steps)


# -- whole-document wrappers (round-trip checks, previews) -----------------------------------

def _dialect(language: str):
    d = Dialect.for_name(language)
    if d is None:
        raise ValueError("Unknown Gherkin language: %s" % language)
    return d


def _indent_block(text: str, indent: str) -> List[str]:
    return [indent + line if line else "" for line in text.split("\n")] if text else []


def _header(language: str, title: str) -> List[str]:
    d = _dialect(language)
    out = ["# language: %s" % language] if language != "en" else []
    out.append("%s: %s" % (d.feature_keywords[0], title))
    out.append("")
    return out


def scenario_as_feature(s: ParsedScenario, language: str = "en", title: str = "Round trip") -> str:
    """A complete single-scenario feature document around scenario_definition(s)."""
    d = _dialect(language)
    keyword = s.keyword or (d.scenario_outline_keywords[0] if s.is_outline else d.scenario_keywords[0])
    out = _header(language, title)
    out.append(INDENT + "%s: %s" % (keyword, s.name))
    out.extend(_indent_block(scenario_definition(s, keep_examples_keyword=True), INDENT * 2))
    return "\n".join(out) + "\n"


def background_as_feature(b: ParsedBackground, language: str = "en", title: str = "Round trip") -> str:
    """A complete feature document holding only this Background."""
    d = _dialect(language)
    out = _header(language, title)
    out.append(INDENT + "%s: %s" % (b.keyword or d.background_keywords[0], b.name))
    out.extend(_indent_block(background_definition(b), INDENT * 2))
    return "\n".join(out) + "\n"
