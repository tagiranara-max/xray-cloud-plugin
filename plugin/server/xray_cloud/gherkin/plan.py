"""Plan a feature-file import with Xray-native semantics (spec 6.4a):
Scenario -> Cucumber Test, Background -> Pre-Condition (or inline steps), Feature -> Test Set,
plain tags -> tag rules / labels, issue-key tags -> update/reuse/link existing issues.

analyze_tags validates every tag and applies the tag rules without any call; plan_import
then only reads (duplicate search, Pre-Condition and Test Set matching, key-tag lookups);
tools/features.py executes the plan."""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from ..context import (ISSUE_KEY_RE, ServerContext, check_issue_in_project, dedupe, jql_str,
                       jql_text)
from ..config import ProjectSettings
from ..errors import ToolError
from ..notdone import (NEVER_OVERWRITE_FIX, NO_JIRA_FIX, NO_JIRA_REASON,  # noqa: F401
                       UNREADABLE_FIX, UNREADABLE_REASON, jira_reason_fix, not_done)
from .parse import ParsedBackground, ParsedFeature, ParsedScenario
from .serialize import background_definition, scenario_definition
from .tagrules import FIELD_ID, TagOutcome, TagRule, apply_rules, kv_key, merge_outcomes

CUCUMBER = "Cucumber"
UNNAMED_PREFIX = "Background for: "

# A tag remainder that is meant as an issue key (validated strictly with ISSUE_KEY_RE).
_KEYISH = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")
_PREFIXES = (("TEST_", "test"), ("PRECOND_", "precondition"), ("REQ_", "requirement"))

@dataclass
class ImportOptions:
    """Resolved import choices (tools/features.py resolves them; defaults = today's behaviour)."""
    create_test_set: bool = True
    backgrounds: str = "preconditions"   # preconditions | inline
    tags_as_labels: bool = True
    unmatched_tags: str = "keep"         # keep | skip
    rules: List[TagRule] = field(default_factory=list)
    ai_label: str = ""                   # "" = don't mark
    ai_scope: str = "tests"              # tests | all
    project: Optional[ProjectSettings] = None  # field specs for rule `field` actions

    @property
    def inline(self) -> bool:
        return self.backgrounds == "inline"


@dataclass
class ImportAction:
    kind: str  # create_test|update_test|create_precondition|reuse_precondition|create_test_set|reuse_test_set|link
    summary: str
    key: Optional[str]
    labels: List[str]
    duplicates: List[dict]
    source_line: int
    index: Optional[int] = None          # scenario index (tests) - what createDuplicates refers to
    definition: str = ""                 # Gherkin for tests / Pre-Conditions
    description: str = ""
    requirement_keys: List[str] = field(default_factory=list)
    tests: List[int] = field(default_factory=list)  # scenario indexes this action applies to
    issue_id: Optional[str] = None       # existing issue (update/reuse)
    current_labels: List[str] = field(default_factory=list)  # existing test's labels
    update_definition: bool = False      # reuse_precondition whose definition differs
    unnamed: bool = False                # Pre-Condition of an unnamed Background
    current_summary: Optional[str] = None  # reused Pre-Condition's summary
    precondition: Optional[int] = None   # link: index of the Pre-Condition action in the plan
    test_sets: List[str] = field(default_factory=list)   # tests: Test Set names from tag rules
    fields: Dict[str, Any] = field(default_factory=dict)  # tests: friendly field -> rule value
    field_updates: Dict[str, Any] = field(default_factory=dict)  # existing test: Jira id -> value
    field_names: List[str] = field(default_factory=list)  # existing test: names of field_updates
    not_done: List[Dict[str, str]] = field(default_factory=list)  # predicted skipped items
    feature_set: bool = False            # the Feature's Test Set (vs. one from tags)
    similar: List[dict] = field(default_factory=list)  # new tests: similar, not duplicates

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "summary": self.summary, "line": self.source_line}
        if self.key:
            out["key"] = self.key
        if self.index is not None:
            out["scenarioIndex"] = self.index
        if self.labels:
            out["labels"] = list(self.labels)
        if self.duplicates:
            out["duplicates"] = list(self.duplicates)
        if self.similar:
            out["similar"] = list(self.similar)
        if self.requirement_keys:
            out["requirementKeys"] = list(self.requirement_keys)
        if self.test_sets:
            out["testSets"] = list(self.test_sets)
        if self.fields:
            out["fields"] = dict(self.fields)
        if self.kind in ("create_precondition", "reuse_precondition", "create_test_set",
                         "reuse_test_set", "link"):
            out["scenarioIndexes"] = list(self.tests)
        if self.update_definition:
            out["updateDefinition"] = True
        if self.feature_set:
            out["featureTestSet"] = True
        return out


def path_label(feature_path: str) -> str:
    """Label carrying the feature's relative path (Jira labels cannot contain whitespace)."""
    return re.sub(r"\s+", "_", feature_path.strip().replace("\\", "/"))


def classify_tag(tag: str) -> Tuple[str, str]:
    """('test'|'precondition'|'requirement'|'key'|'label', value). 'key' is a plain issue key.
    A prefixed tag whose rest looks like a key but is not a valid one is refused."""
    name = tag[1:] if tag.startswith("@") else tag
    for prefix, kind in _PREFIXES:
        if name.startswith(prefix) and _KEYISH.match(name[len(prefix):]):
            key = name[len(prefix):]
            if not ISSUE_KEY_RE.fullmatch(key):
                raise ToolError("Invalid issue key in tag %s" % tag, tag=tag)
            return kind, key
    if ISSUE_KEY_RE.fullmatch(name):
        return "key", name
    return "label", name


def _container_tags(tags: List[str], where: str) -> Tuple[List[str], List[str]]:
    """Feature/Rule tags -> (plain tags without "@", requirement keys). @REQ_<KEY> and plain
    keys link."""
    plain, reqs = [], []
    for t in tags:
        kind, value = classify_tag(t)
        if kind == "label":
            plain.append(value)
        elif kind in ("requirement", "key"):
            reqs.append(value)
        else:
            raise ToolError("Tag %s is not allowed on a %s (use @TEST_ on scenarios and "
                            "#@PRECOND_ comments in Backgrounds)" % (t, where), tag=t)
    return plain, reqs


def _scenario_tags(s: ParsedScenario, project_key: str) -> Tuple[List[str], List[str], Optional[str]]:
    """-> (plain tags without "@", requirement keys, test key)."""
    plain, reqs, test_keys = [], [], []
    for t in s.tags:
        kind, value = classify_tag(t)
        if kind == "label":
            plain.append(value)
        elif kind == "requirement":
            reqs.append(value)
        elif kind in ("test", "key"):
            if value.rsplit("-", 1)[0] != project_key:
                raise ToolError(
                    "Tag %s on scenario %r (line %d) names a test outside project %s; tests can "
                    "only be updated in the import's project (use @REQ_%s to link a requirement)"
                    % (t, s.name, s.line, project_key, value),
                    tag=t, line=s.line, projectKey=project_key)
            test_keys.append(value)
        else:
            raise ToolError("Tag %s on scenario %r (line %d): Pre-Condition keys belong in the "
                            "Background as a comment (#@PRECOND_<KEY>)" % (t, s.name, s.line),
                            tag=t, line=s.line)
    test_keys = dedupe(test_keys)
    if len(test_keys) > 1:
        raise ToolError("Scenario %r (line %d) has more than one test key tag: %s"
                        % (s.name, s.line, ", ".join(test_keys)), line=s.line)
    return plain, reqs, (test_keys[0] if test_keys else None)


def _precond_key(bg: ParsedBackground, project_key: str) -> Optional[str]:
    keys = []
    for t in bg.comment_tags:
        name = t[1:]
        if not name.startswith("PRECOND_"):
            continue
        kind, value = classify_tag(t)
        if kind != "precondition":
            raise ToolError("Invalid issue key in tag %s" % t, tag=t, line=bg.line)
        check_issue_in_project(value, project_key)
        keys.append(value)
    keys = dedupe(keys)
    if len(keys) > 1:
        raise ToolError("Background at line %d has more than one Pre-Condition key: %s"
                        % (bg.line, ", ".join(keys)), line=bg.line)
    return keys[0] if keys else None


# ---- reads -------------------------------------------------------------------------------

_GET_PRECONDITIONS = """
query GetPreconditions($jql: String!, $limit: Int!) {
  getPreconditions(jql: $jql, limit: $limit) {
    results { issueId preconditionType { name } definition jira(fields: ["key", "summary", "labels"]) }
  }
}
"""
_GET_TEST_SETS = """
query FindTestSets($jql: String!, $limit: Int!, $start: Int) {
  getTestSets(jql: $jql, limit: $limit, start: $start) {
    results { issueId jira(fields: ["key", "summary"]) }
  }
}
"""


def find_preconditions(ctx: ServerContext, project_key: str, jql: str, limit: int = 100) -> List[dict]:
    data = ctx.xray.graphql(_GET_PRECONDITIONS, {"jql": jql, "limit": limit})
    out = []
    for r in ((data.get("getPreconditions") or {}).get("results")) or []:
        j = r.get("jira") or {}
        if not str(j.get("key", "")).startswith(project_key + "-"):
            continue
        pt = r.get("preconditionType")
        out.append({"issueId": r.get("issueId"), "key": j.get("key"),
                    "summary": j.get("summary") or "", "labels": j.get("labels") or [],
                    "definition": r.get("definition") or "",
                    "type": pt.get("name") if isinstance(pt, dict) else pt})
    return out


def _same_definition(a: str, b: str) -> bool:
    return a.strip() == b.strip()


def _scenario_summary(s: ParsedScenario, feature: ParsedFeature) -> str:
    if s.name.strip():
        return s.name.strip()
    return "%s (line %d)" % (feature.name.strip() or "Scenario", s.line)


# ---- tag analysis (no calls) ---------------------------------------------------------------

@dataclass
class ScenarioTags:
    outcome: TagOutcome        # feature + rule + scenario tags through the rules, merged
    requirement_keys: List[str]
    test_key: Optional[str]


@dataclass
class TagAnalysis:
    feature: TagOutcome        # the Feature's own tags (labels go onto the Feature Test Set)
    scenarios: List[ScenarioTags]
    bg_keys: List[Optional[str]]  # #@PRECOND_ key per Background (None when inline)
    plain_tags: List[str]      # distinct non-issue-key tags (without "@")
    any_label: bool            # some plain tag no rule covers would be kept as a label
    warnings: List[str]
    # key:value tags no rule covers: lower-cased key -> distinct tags (without "@"), in
    # first-seen order; the import asks what each key means (empty with unmatchedTags=skip)
    uncovered: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def tag_sets(self) -> List[str]:
        return dedupe([n for st in self.scenarios for n in st.outcome.test_sets])


def _outcome(tags: List[str], opts: ImportOptions, tags_as_labels: bool) -> TagOutcome:
    return apply_rules(tags, opts.rules, tags_as_labels, opts.unmatched_tags)


def analyze_tags(feature: ParsedFeature, project_key: str,
                 opts: Optional[ImportOptions] = None) -> TagAnalysis:
    """Validate every tag and apply the tag rules (feature, rule and scenario tags; Examples
    tags are not used). Makes no call. Invalid rule results (labels, field conflicts) refuse
    the whole file, naming tag, rule and result."""
    opts = opts or ImportOptions()
    feat_plain, feat_reqs = _container_tags(feature.tags, "Feature")
    feat_out = _outcome(feat_plain, opts, opts.tags_as_labels)
    plain_all: List[str] = list(feat_plain)
    any_label = _keeps_label(feat_out, opts)
    scenarios: List[ScenarioTags] = []
    seen_keys: Dict[str, int] = {}
    problems: List[str] = []
    for i, s in enumerate(feature.scenarios):
        # Per scenario, not cached by rule name: Rules may share a name or be unnamed.
        r_plain, r_reqs = _container_tags(s.rule_tags, "Rule")
        plain, reqs, test_key = _scenario_tags(s, project_key)
        if test_key and test_key in seen_keys:
            raise ToolError("Test key %s is tagged on more than one scenario (lines %d and %d)"
                            % (test_key, feature.scenarios[seen_keys[test_key]].line, s.line))
        if test_key:
            seen_keys[test_key] = i
        plain_all.extend(r_plain + plain)
        merged = merge_outcomes(feat_out, _outcome(r_plain, opts, opts.tags_as_labels),
                                _outcome(plain, opts, opts.tags_as_labels))
        any_label = any_label or _keeps_label(merged, opts)
        problems.extend("scenario %r (line %d): %s" % (s.name, s.line, p)
                        for p in merged.problems if p not in feat_out.problems)
        scenarios.append(ScenarioTags(merged, dedupe(feat_reqs + r_reqs + reqs), test_key))
    problems = feat_out.problems + problems
    if problems:
        raise ToolError("Tag rules give invalid results; nothing was imported: "
                        + "; ".join(dedupe(problems)), problems=dedupe(problems))
    warnings: List[str] = []
    if opts.inline:
        bg_keys: List[Optional[str]] = [None] * len(feature.backgrounds)
        for bg in feature.backgrounds:
            pre = [t for t in bg.comment_tags if t[1:].startswith("PRECOND_")]
            if pre:
                warnings.append("%s in the Background at line %d ignored: Backgrounds are "
                                "inlined into the tests" % (", ".join("#" + t for t in pre),
                                                             bg.line))
    else:
        bg_keys = [_precond_key(bg, project_key) for bg in feature.backgrounds]
        dup_bg = [k for k in bg_keys if k and bg_keys.count(k) > 1]
        if dup_bg:
            raise ToolError("Pre-Condition key %s is used by more than one Background"
                            % dup_bg[0])
    uncovered: Dict[str, List[str]] = {}
    if opts.unmatched_tags == "keep":
        for tag in dedupe(plain_all):
            key = kv_key(tag)
            if key is not None and tag in _outcome([tag], opts, False).unmatched:
                uncovered.setdefault(key, []).append(tag)
    return TagAnalysis(feat_out, scenarios, bg_keys, dedupe(plain_all), any_label, warnings,
                       uncovered)


def _keeps_label(outcome: TagOutcome, opts: ImportOptions) -> bool:
    """Whether a plain tag no rule covers would be kept as a label (tagsAsLabels=true)."""
    return opts.unmatched_tags == "keep" and any(kv_key(t) is None for t in outcome.unmatched)


def feature_set_name(feature: ParsedFeature, feature_path: str) -> str:
    return feature.name.strip() or path_label(feature_path)


def test_definition(s: ParsedScenario, inline: bool) -> str:
    """The scenario's Xray definition; inline: feature Background steps, then rule Background
    steps, then the scenario's own steps (and Examples)."""
    if inline and s.backgrounds:
        s = dataclasses.replace(s, steps=[st for bg in s.backgrounds for st in bg.steps]
                                + list(s.steps))
    return scenario_definition(s)


# ---- planning ----------------------------------------------------------------------------

_SET_PAGE = 100       # Xray's page size limit
MAX_SET_PAGES = 20    # at most 2000 fuzzy `summary ~` hits are checked for the exact name


def find_test_sets(ctx: ServerContext, project_key: str, summary: str) -> List[dict]:
    """Every Test Set in the project with exactly this summary. `summary ~` is fuzzy, so the
    hits are paged until a short page; past MAX_SET_PAGES the import is refused rather than
    risk creating a second Test Set with the same name."""
    term = jql_text(summary)
    if term is None:
        return []
    jql = "project = %s AND summary ~ %s" % (project_key, term)
    out = []
    for page in range(MAX_SET_PAGES):
        data = ctx.xray.graphql(_GET_TEST_SETS, {"jql": jql, "limit": _SET_PAGE,
                                                 "start": page * _SET_PAGE})
        results = ((data.get("getTestSets") or {}).get("results")) or []
        for r in results:
            j = r.get("jira") or {}
            if str(j.get("key", "")).startswith(project_key + "-") and \
                    (j.get("summary") or "").strip() == summary.strip():
                out.append({"issueId": r["issueId"], "key": j["key"], "summary": j["summary"]})
        if len(results) < _SET_PAGE:
            return out
    raise ToolError("Cannot tell whether Test Set %r exists: more than %d Test Sets in %s match "
                    "it in a text search; nothing was imported. Use a more distinctive Test Set "
                    "name (Feature name or tag rule)" % (summary, MAX_SET_PAGES * _SET_PAGE,
                                                         project_key), testSet=summary)


MAX_NAME_CANDIDATES = 50
_NAME_CHUNK = 10


def existing_set_names(ctx: ServerContext, project_key: str, candidates: List[str]) -> Set[str]:
    """Which of these names are the exact summary of an existing Test Set in the project.
    A project-wide listing is unbounded, so the (at most 50) candidates are searched in
    OR-ed `summary ~` queries of 10 (at most 5 reads) and matched exactly here."""
    wanted = {c.strip(): jql_text(c) for c in candidates[:MAX_NAME_CANDIDATES] if c.strip()}
    terms = [(n, t) for n, t in wanted.items() if t]
    found: Set[str] = set()
    for i in range(0, len(terms), _NAME_CHUNK):
        chunk = terms[i:i + _NAME_CHUNK]
        data = ctx.xray.graphql(_GET_TEST_SETS, {
            "jql": "project = %s AND (%s)" % (project_key,
                                              " OR ".join("summary ~ %s" % t for _, t in chunk)),
            "limit": 100})
        for r in ((data.get("getTestSets") or {}).get("results")) or []:
            j = r.get("jira") or {}
            name = (j.get("summary") or "").strip()
            if str(j.get("key", "")).startswith(project_key + "-") and name in wanted:
                found.add(name)
    return found


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _show(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_show(v) for v in value)
    if isinstance(value, dict):
        for k in ("value", "name", "displayName", "key", "id"):
            if value.get(k) is not None:
                return str(value[k])
    return str(value)


def _same_value(wanted: Any, current: Any) -> bool:
    """Whether the converted rule value equals Jira's current value. Jira returns more keys
    than are written (an option's id, a user's displayName), so dicts match on the keys
    written; anything else compares as shown."""
    if isinstance(wanted, dict) and isinstance(current, dict):
        return all(current.get(k) == v for k, v in wanted.items())
    if isinstance(wanted, list) and isinstance(current, list):
        return len(wanted) == len(current) and all(
            any(_same_value(w, c) for c in current) for w in wanted)
    return _show(wanted) == _show(current)


def _field_id(opts: ImportOptions, name: str) -> Optional[str]:
    """Jira id of a rule field name: a fields.test spec's id, or a customfield_<n> id."""
    specs = (opts.project.fields.get("test") or {}) if opts.project else {}
    if name in specs:
        return specs[name].id
    return name if FIELD_ID.fullmatch(name) else None


UNTYPED_REASON = ("its type is unknown: Jira's create metadata for Test does not list it "
                  "(or could not be read)")
UNTYPED_FIX = ("name the field in fields.test of the settings file with its type and use that "
               "name in the tag rule")


def _existing_test_fields(ctx: ServerContext, a: ImportAction, current: Dict[str, Any],
                          opts: ImportOptions, has_jira: bool) -> None:
    """Rule fields on an existing test: set via Jira only when empty; never overwritten. An
    equal value is left alone silently; a value that could not be read is never written. A
    customfield_<n> id the settings do not name is converted by Jira's create metadata, like
    for new tests; without metadata for it, it is never written (notDone)."""
    from ..fields import build_jira_fields
    from ..requiredfields import _convert_meta, _lookup, _meta
    from ..tools.tests import _lenient
    specs = (opts.project.fields.get("test") or {}) if opts.project else {}
    lenient = _lenient(opts.project, "test") if opts.project else None
    meta = _meta(ctx, opts.project.key, "test") if has_jira and opts.project else None
    todo: Dict[str, Any] = {}   # Jira id -> converted value
    names: List[str] = []
    shown: List[str] = []
    for name, value in a.fields.items():
        fid = _field_id(opts, name)
        if not fid:
            continue
        what = "field %s = %s" % (name, value)
        if name in specs:
            wanted = build_jira_fields(lenient, "test", {name: value}, None)[fid]
        else:
            f = _lookup(meta, fid)
            if f is None and has_jira:
                a.not_done.append(not_done(a.key, what, UNTYPED_REASON, UNTYPED_FIX))
                continue
            wanted = _convert_meta(f, value) if f is not None else value
        if fid not in current:
            a.not_done.append(not_done(a.key, what, UNREADABLE_REASON, UNREADABLE_FIX))
        elif _is_empty(current[fid]):
            todo[fid] = wanted
            names.append(name)
            shown.append("%s = %s" % (name, value))
        elif not _same_value(wanted, current[fid]):
            a.not_done.append(not_done(a.key, what, "already set to %s" % _show(current[fid]),
                                       NEVER_OVERWRITE_FIX))
    if not todo:
        return
    if not has_jira:
        a.not_done.append(not_done(a.key, "fields " + ", ".join(shown),
                                   *jira_reason_fix(ctx.env)))
        return
    a.field_updates = todo
    a.field_names = names


def plan_import(ctx: ServerContext, project_key: str, feature: ParsedFeature, feature_path: str,
                options: Union[bool, ImportOptions] = True,
                analysis: Optional[TagAnalysis] = None) -> List[ImportAction]:
    """The import plan, in execution order: tests (create/update), Pre-Conditions
    (create/reuse), links (Pre-Condition -> tests, requirement <- tests), Test Sets (the
    Feature's first, then those from tags). `options` may be a bool (createTestSet, with
    today's defaults otherwise). Validates every tag first (no calls), then reads; never
    writes."""
    from ..tools.tests import check_duplicates, fetch_test  # tools import gherkin, not vice versa

    opts = ImportOptions(create_test_set=options) if isinstance(options, bool) else options
    if not feature.scenarios:
        raise ToolError("No scenarios found in %s" % feature_path, path=feature_path)
    label = path_label(feature_path)
    if analysis is None:
        analysis = analyze_tags(feature, project_key, opts)
    has_jira = ctx.jira is not None

    # -- tests
    tests: List[ImportAction] = []
    for i, s in enumerate(feature.scenarios):
        st = analysis.scenarios[i]
        summary = _scenario_summary(s, feature)
        a = ImportAction("create_test", summary, None, list(st.outcome.labels), [], s.line,
                         index=i, definition=test_definition(s, opts.inline),
                         description=s.description, requirement_keys=list(st.requirement_keys),
                         test_sets=list(st.outcome.test_sets), fields=dict(st.outcome.fields))
        if st.test_key:
            ids = [fid for fid in (_field_id(opts, n) for n in a.fields) if fid]
            r = fetch_test(ctx, project_key, st.test_key, jira_fields=ids)
            tt = r.get("testType")
            tt = tt.get("name") if isinstance(tt, dict) else tt
            if tt != CUCUMBER:
                raise ToolError("Test %s is a %s test; only Cucumber tests can take a scenario "
                                "definition" % (st.test_key, tt), testKey=st.test_key, testType=tt)
            a.kind, a.key, a.issue_id = "update_test", st.test_key, r["issueId"]
            current = r.get("jira") or {}
            a.current_labels = list(current.get("labels") or [])
            new_labels = [x for x in a.labels if x not in a.current_labels]
            if new_labels and not has_jira:
                a.not_done.append(not_done(a.key, "labels " + ", ".join(new_labels),
                                           *jira_reason_fix(ctx.env)))
            _existing_test_fields(ctx, a, current, opts, has_jira)
            if a.requirement_keys and not has_jira:
                a.not_done.append(not_done(a.key, "requirement links "
                                           + ", ".join(a.requirement_keys),
                                           *jira_reason_fix(ctx.env)))
        else:
            if opts.ai_label:
                a.labels = dedupe(a.labels + [opts.ai_label])
            a.duplicates, a.similar = check_duplicates(ctx, project_key, summary)
        tests.append(a)

    # -- Pre-Conditions: key tag -> path label -> same summary -> create
    pre: List[ImportAction] = []
    by_label: Optional[List[dict]] = None
    used: set = set()
    backgrounds = [] if opts.inline else feature.backgrounds
    unnamed_count = sum(1 for b in backgrounds if not b.name.strip())
    for bg, key in zip(backgrounds, analysis.bg_keys):
        applies = [i for i, s in enumerate(feature.scenarios)
                   if any(b is bg for b in s.backgrounds)]
        definition = background_definition(bg)
        name = bg.name.strip()
        labels = [label] + ([opts.ai_label] if opts.ai_label and opts.ai_scope == "all" else [])
        a = ImportAction("create_precondition", name, None, labels, [], bg.line,
                         definition=definition, description=bg.description, tests=applies,
                         unnamed=not name)
        match = None
        if key:
            found = find_preconditions(ctx, project_key, "issue = %s" % key, 1)
            if not found:
                raise ToolError("Pre-Condition not found: %s" % key, key=key, line=bg.line)
            match = found[0]
            if match["type"] != CUCUMBER:
                raise ToolError("Pre-Condition %s is of type %s; Cucumber tests need a Cucumber "
                                "Pre-Condition" % (key, match["type"]), key=key)
        else:
            if by_label is None:
                by_label = [c for c in find_preconditions(
                    ctx, project_key, "project = %s AND labels = %s" % (project_key, jql_str(label)))
                    if c["type"] == CUCUMBER]
            free = [c for c in by_label if c["key"] not in used and label in c["labels"]]
            if name:
                match = next((c for c in free if c["summary"].strip() == name), None)
            else:
                unnamed = [c for c in free if c["summary"].startswith(UNNAMED_PREFIX)]
                match = next((c for c in unnamed if _same_definition(c["definition"], definition)),
                             None)
                if match is None and unnamed_count == 1 and len(unnamed) == 1:
                    match = unnamed[0]
            if match is None and name and jql_text(name):
                match = next((c for c in find_preconditions(
                    ctx, project_key, "project = %s AND summary ~ %s" % (project_key, jql_text(name)))
                    if c["type"] == CUCUMBER and c["key"] not in used
                    and c["summary"].strip() == name), None)
        if match is not None:
            used.add(match["key"])
            a.kind, a.key, a.issue_id = "reuse_precondition", match["key"], match["issueId"]
            a.current_summary = match["summary"]
            a.labels = []
            a.update_definition = not _same_definition(match["definition"], definition)
            if not name:
                a.summary = match["summary"]
        if not name and a.kind == "create_precondition":
            a.summary = UNNAMED_PREFIX + "<keys of its tests>"
        pre.append(a)

    # -- links (informational for the preview; executed with the Pre-Conditions / tests)
    links: List[ImportAction] = []
    for n, p in enumerate(pre):
        links.append(ImportAction("link", "Pre-Condition %s -> %d test(s)"
                                  % (p.key or p.summary or "(new)", len(p.tests)),
                                  p.key, [], [], p.source_line, tests=list(p.tests),
                                  precondition=len(tests) + n))
    reqs_order = dedupe([k for t in tests for k in t.requirement_keys])
    for k in reqs_order:
        idx = [t.index for t in tests if k in t.requirement_keys]
        links.append(ImportAction("link", "Requirement %s <- %d test(s) (Tests link)" % (k, len(idx)),
                                  k, [], [], feature.line, requirement_keys=[k], tests=idx))

    # -- Test Sets: the Feature's (createTestSet) and those named by tag rules; one per name
    members: Dict[str, List[int]] = {}
    if opts.create_test_set:
        members[feature_set_name(feature, feature_path)] = list(range(len(feature.scenarios)))
    for i, t in enumerate(tests):
        for n in t.test_sets:
            if i not in members.setdefault(n, []):
                members[n].append(i)
    ai_on_sets = [opts.ai_label] if opts.ai_label and opts.ai_scope == "all" else []
    sets: List[ImportAction] = []
    for n, (name, idx) in enumerate(members.items()):
        is_feature = opts.create_test_set and n == 0
        found = find_test_sets(ctx, project_key, name)
        if len(found) > 1:
            raise ToolError("Test Set name %r is ambiguous: %d Test Sets in %s have it (%s); "
                            "rename all but one, or change the tag or rule"
                            % (name, len(found), project_key,
                               ", ".join(f["key"] for f in found)),
                            testSet=name, keys=[f["key"] for f in found])
        a = ImportAction("create_test_set", name, None,
                         dedupe((analysis.feature.labels if is_feature else []) + ai_on_sets),
                         [], feature.line,
                         description=feature.description if is_feature else "",
                         tests=sorted(idx), feature_set=is_feature)
        if found:
            a.kind, a.key, a.issue_id = "reuse_test_set", found[0]["key"], found[0]["issueId"]
            a.labels = []
        sets.append(a)
    return tests + pre + links + sets
