"""Parse a Gherkin .feature file into plain dataclasses using the vendored official
Cucumber parser (``xray_cloud/_vendor/gherkin``). Every keyword and language is supported;
scenarios inside ``Rule:`` are kept, and each scenario carries the Backgrounds that apply to
it (feature-level first, then its rule's)."""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from typing import List, Optional

from .._vendor_path import import_vendored
from ..errors import ToolError

# The vendored, conformance-tested parser always wins over an installed gherkin.
import_vendored("gherkin")

from gherkin.dialect import Dialect  # noqa: E402
from gherkin.errors import CompositeParserException, ParserException  # noqa: E402
from gherkin.parser import Parser  # noqa: E402
from gherkin.token_matcher import TokenMatcher  # noqa: E402

_LOCATION_PREFIX = re.compile(r"^\(\d+:\d+\): ")


class GherkinError(ToolError):
    """A .feature file that does not parse. data: source, line, column (>= 1, of the first
    error) and errors: [{message, line, column}] for every error the parser reported."""

    def __init__(self, message: str, source: str, line: int, column: int,
                 errors: Optional[list] = None) -> None:
        super().__init__(message, source=source, line=line, column=column,
                         errors=errors if errors is not None else
                         [{"message": message, "line": line, "column": column}])


@dataclass
class DocString:
    content: str
    delimiter: str = '"""'
    media_type: Optional[str] = None


@dataclass
class ParsedStep:
    keyword: str  # as written, including its trailing space ("Given ", "* ")
    text: str
    data_table: Optional[List[List[str]]] = None  # every row, header included; unescaped cells
    doc_string: Optional[DocString] = None


@dataclass
class ParsedExamples:
    name: str
    description: str
    tags: List[str]  # with "@"
    header: List[str]
    rows: List[List[str]]
    keyword: str = "Examples"
    line: int = 0


@dataclass
class ParsedBackground:
    name: str
    description: str
    steps: List[ParsedStep]
    scope: str  # "feature" | "rule"
    keyword: str = "Background"
    line: int = 0
    rule: Optional[str] = None  # rule name when scope == "rule"
    # Tags written in comments inside the Background (Gherkin forbids real tags there), e.g.
    # Xray's "#@PRECOND_CALC-2"; with "@". Collected from the Background line up to the next
    # Scenario/Rule/Background (its tags included).
    comment_tags: List[str] = field(default_factory=list)


@dataclass
class ParsedScenario:
    name: str
    description: str
    tags: List[str]  # the scenario's own tags, with "@"
    steps: List[ParsedStep]
    examples: List[ParsedExamples]
    is_outline: bool
    rule: Optional[str]
    backgrounds: List[ParsedBackground]  # applicable ones: feature-level first, then rule-level
    line: int
    keyword: str = "Scenario"
    rule_tags: List[str] = field(default_factory=list)


@dataclass
class ParsedFeature:
    name: str
    description: str
    tags: List[str]
    language: str
    scenarios: List[ParsedScenario]
    keyword: str = "Feature"
    backgrounds: List[ParsedBackground] = field(default_factory=list)  # all, in file order
    line: int = 0


def normalise_text(text: str) -> str:
    """Strip one leading BOM, turn CRLF and lone CR into LF; everything else is kept exactly."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_feature(text: str, source: str) -> ParsedFeature:
    """Parse one .feature document. Raises GherkinError (with location) if it is invalid.
    A document without a Feature (empty, comments only) yields a feature with no scenarios."""
    text = normalise_text(text)
    try:
        doc = Parser().parse(text, TokenMatcher())
    except CompositeParserException as exc:
        raise _error(source, list(exc.errors)) from None
    except ParserException as exc:
        raise _error(source, [exc]) from None
    feature = doc.get("feature")
    if not feature:
        return ParsedFeature(name="", description="", tags=[], language="en", scenarios=[])
    parsed = _feature(feature)
    _attach_comment_tags(parsed, feature, doc.get("comments") or [])
    return parsed


_COMMENT_TAGS = re.compile(r"^\s*#\s*(@\S+(?:\s+@\S+)*)\s*$")


def _start_line(node: dict) -> int:
    lines = [node["location"]["line"]] + [t["location"]["line"] for t in node.get("tags") or []]
    return min(lines)


def _attach_comment_tags(parsed: ParsedFeature, feature: dict, comments: list) -> None:
    """Give each Background the tags written in comments between it and the next child."""
    if not parsed.backgrounds:
        return
    starts = []
    for child in feature.get("children") or []:
        node = next(iter(child.values()))
        starts.append(_start_line(node))
        if "rule" in child:
            starts.extend(_start_line(next(iter(r.values())))
                          for r in child["rule"].get("children") or [])
    for bg in parsed.backgrounds:
        end = min([s for s in starts if s > bg.line] or [float("inf")])
        for c in comments:
            line = c["location"]["line"]
            m = _COMMENT_TAGS.match(c.get("text") or "")
            if m and bg.line < line < end:
                bg.comment_tags.extend(m.group(1).split())


def _error(source: str, errors: list) -> GherkinError:
    items = []
    for e in errors:
        loc = getattr(e, "location", None) or {}
        msg = e.args[0] if e.args else str(e)
        items.append({
            "message": _LOCATION_PREFIX.sub("", str(msg)),
            "line": max(1, int(loc.get("line") or 1)),
            "column": max(1, int(loc.get("column") or 1)),
        })
    if not items:  # defensive: never raise without a location
        items.append({"message": "Gherkin parse error", "line": 1, "column": 1})
    first = items[0]
    message = "Gherkin parse error in %s at line %d, column %d: %s" % (
        source, first["line"], first["column"], first["message"])
    if len(items) > 1:
        message += " (and %d more error%s)" % (len(items) - 1, "s" if len(items) > 2 else "")
    return GherkinError(message, source, first["line"], first["column"], items)


def _description(node: dict) -> str:
    text = node.get("description") or ""
    return textwrap.dedent(text).strip("\n")


def _tags(node: dict) -> List[str]:
    return [t["name"] for t in node.get("tags") or []]


def _cells(row: dict) -> List[str]:
    return [c["value"] for c in row.get("cells") or []]


def _step(node: dict) -> ParsedStep:
    table = node.get("dataTable")
    ds = node.get("docString")
    return ParsedStep(
        keyword=node["keyword"],
        text=node["text"],
        data_table=[_cells(r) for r in table["rows"]] if table else None,
        doc_string=DocString(ds["content"], ds.get("delimiter") or '"""', ds.get("mediaType") or None)
        if ds else None,
    )


def _background(node: dict, scope: str, rule: Optional[str]) -> ParsedBackground:
    return ParsedBackground(
        name=node.get("name") or "",
        description=_description(node),
        steps=[_step(s) for s in node.get("steps") or []],
        scope=scope,
        keyword=node.get("keyword") or "Background",
        line=node["location"]["line"],
        rule=rule,
    )


def _examples(node: dict) -> ParsedExamples:
    header = node.get("tableHeader")
    return ParsedExamples(
        name=node.get("name") or "",
        description=_description(node),
        tags=_tags(node),
        header=_cells(header) if header else [],
        rows=[_cells(r) for r in node.get("tableBody") or []],
        keyword=node.get("keyword") or "Examples",
        line=node["location"]["line"],
    )


def _scenario(node: dict, outline_keywords: List[str], rule: Optional[str],
              rule_tags: List[str], backgrounds: List[ParsedBackground]) -> ParsedScenario:
    examples = [_examples(e) for e in node.get("examples") or []]
    keyword = node.get("keyword") or "Scenario"
    return ParsedScenario(
        name=node.get("name") or "",
        description=_description(node),
        tags=_tags(node),
        steps=[_step(s) for s in node.get("steps") or []],
        examples=examples,
        is_outline=bool(examples) or keyword in outline_keywords,
        rule=rule,
        backgrounds=list(backgrounds),
        line=node["location"]["line"],
        keyword=keyword,
        rule_tags=list(rule_tags),
    )


def _feature(feature: dict) -> ParsedFeature:
    language = feature.get("language") or "en"
    dialect = Dialect.for_name(language)
    outline_keywords = list(dialect.scenario_outline_keywords) if dialect else []

    all_backgrounds: List[ParsedBackground] = []
    feature_bgs: List[ParsedBackground] = []
    scenarios: List[ParsedScenario] = []
    for child in feature.get("children") or []:
        if "background" in child:
            bg = _background(child["background"], "feature", None)
            feature_bgs.append(bg)
            all_backgrounds.append(bg)
        elif "scenario" in child:
            scenarios.append(_scenario(child["scenario"], outline_keywords, None, [], feature_bgs))
        elif "rule" in child:
            rule = child["rule"]
            rule_name = rule.get("name") or ""
            rule_tags = _tags(rule)
            rule_bgs: List[ParsedBackground] = []
            for rchild in rule.get("children") or []:
                if "background" in rchild:
                    bg = _background(rchild["background"], "rule", rule_name)
                    rule_bgs.append(bg)
                    all_backgrounds.append(bg)
                elif "scenario" in rchild:
                    scenarios.append(_scenario(rchild["scenario"], outline_keywords, rule_name,
                                               rule_tags, feature_bgs + rule_bgs))
    return ParsedFeature(
        name=feature.get("name") or "",
        description=_description(feature),
        tags=_tags(feature),
        language=language,
        scenarios=scenarios,
        keyword=feature.get("keyword") or "Feature",
        backgrounds=all_backgrounds,
        line=feature["location"]["line"],
    )
