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
    # what each issue-key tag names (lookup_key_tags); None = not looked up: every key is
    # treated as unknown (bare keys are plain tags, @TEST_ keys are not imported)
    key_info: Optional[Dict[str, "KeyInfo"]] = None

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
    reason: str = ""                     # skip_test: why the scenario is not imported
    # existing test (update_test): what it has now, to tell what the import would change
    current_definition: Optional[str] = None
    current_set_ids: Optional[List[str]] = None
    current_pre_ids: Optional[List[str]] = None
    new_links: List[str] = field(default_factory=list)   # with Jira: links not there yet

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
        if self.reason:
            out["reason"] = self.reason
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


@dataclass
class KeyInfo:
    """What an issue-key tag names: test (an Xray test; `project` is its project), issue
    (an existing non-test issue, Jira), missing (Jira has no such issue) or unknown (not an
    Xray test, and no Jira access to tell more)."""
    status: str
    issue_type: str = ""


def _project_of(key: str) -> str:
    return key.rsplit("-", 1)[0]


def _scenario_item(s: ParsedScenario) -> str:
    return "scenario %r (line %d)" % (s.name, s.line)


def _key_meaning(kind: str, key: str, scenario: bool, project_key: str,
                 info: Optional[KeyInfo]) -> Tuple[str, str]:
    """(meaning, why) of an issue-key tag: test (names the scenario's test), otherTest (a
    test of another project), requirement (link), plain (an ordinary tag), notImported
    (@TEST_ that is no test of the project) or missingRequirement (@REQ_ of no issue)."""
    status = info.status if info else "unknown"
    if kind == "requirement":
        if status == "missing":
            return "missingRequirement", "Jira has no issue %s (or the token cannot see it)" % key
        return "requirement", ""
    if kind == "test":
        if status == "test" and _project_of(key) == project_key:
            return "test", ""
        if status == "test":
            why = "%s is a test of project %s; tests can only be updated in the import's " \
                  "project %s" % (key, _project_of(key), project_key)
        elif status == "issue":
            why = "%s is a %s, not a test" % (key, info.issue_type or "issue")
        elif status == "missing":
            why = "Jira has no issue %s (or the token cannot see it)" % key
        else:
            why = "no test %s was found in Xray" % key
        return "notImported", why
    # a bare key
    if status == "test" and scenario:
        if _project_of(key) == project_key:
            return "test", ""
        return "otherTest", ("%s is a test of project %s; it cannot be updated from an import "
                             "into %s, so the scenario becomes a new test"
                             % (key, _project_of(key), project_key))
    if status in ("test", "issue"):
        return "requirement", ""
    if status == "missing":
        return "plain", "not an issue (Jira has no %s): an ordinary tag" % key
    return "plain", ("not an issue (no Xray test %s; without Jira access it cannot be checked "
                     "further): an ordinary tag" % key)


@dataclass
class _Tags:
    plain: List[str] = field(default_factory=list)
    reqs: List[str] = field(default_factory=list)
    test_keys: List[str] = field(default_factory=list)
    notes: List[Dict[str, str]] = field(default_factory=list)   # notDone entries
    blocked: List[str] = field(default_factory=list)            # why not imported
    keys: List[Dict[str, str]] = field(default_factory=list)    # tag classifications


def _classify_keys(tags: List[str], project_key: str, info: Dict[str, KeyInfo],
                   s: Optional[ParsedScenario], where: str) -> _Tags:
    """Feature/Rule (s is None) or scenario tags -> plain tags (without "@"), requirement keys,
    the test key and per-tag notes. Issue keys mean what the lookup found; a key-shaped tag
    never refuses the file. Other prefixed keys are refused where they do not belong."""
    out = _Tags()
    for t in tags:
        kind, value = classify_tag(t)
        if kind == "label":
            out.plain.append(value)
            continue
        if kind == "precondition" or (kind == "test" and s is None):
            if s is None:
                raise ToolError("Tag %s is not allowed on a %s (use @TEST_ on scenarios and "
                                "#@PRECOND_ comments in Backgrounds)" % (t, where), tag=t)
            raise ToolError("Tag %s on scenario %r (line %d): Pre-Condition keys belong in the "
                            "Background as a comment (#@PRECOND_<KEY>)" % (t, s.name, s.line),
                            tag=t, line=s.line)
        meaning, why = _key_meaning(kind, value, s is not None, project_key, info.get(value))
        entry = {"tag": t[1:] if t.startswith("@") else t, "meaning": meaning}
        if why:
            entry["note"] = why
        out.keys.append(entry)
        item = _scenario_item(s) if s is not None else "%s tag %s" % (where, t)
        if meaning == "test":
            out.test_keys.append(value)
        elif meaning == "requirement":
            out.reqs.append(value)
        elif meaning == "plain":
            out.plain.append(value)
        elif meaning == "otherTest":
            out.notes.append(not_done(item, "tag %s" % t, why,
                                      "the scenario is created as a new test in %s; to link it "
                                      "to %s use @REQ_%s" % (project_key, value, value)))
        elif meaning == "notImported":
            out.blocked.append("tag %s: %s" % (t, why))
            out.notes.append(not_done(item, "scenario not imported (tag %s)" % t, why,
                                      "correct the key, or remove the tag to create a new test"))
        else:  # missingRequirement
            out.notes.append(not_done(item, "requirement link %s" % value, why,
                                      "correct the key, or link the requirement later"))
    return out


def _scenario_tags(s: ParsedScenario, project_key: str,
                   info: Dict[str, KeyInfo]) -> Tuple[_Tags, Optional[str]]:
    """-> (classified tags, test key)."""
    out = _classify_keys(s.tags, project_key, info, s, "scenario")
    test_keys = dedupe(out.test_keys)
    if len(test_keys) > 1:
        raise ToolError("Scenario %r (line %d) has more than one test key tag: %s"
                        % (s.name, s.line, ", ".join(test_keys)), line=s.line)
    return out, (test_keys[0] if test_keys and not out.blocked else None)


def key_tags_to_look_up(feature: ParsedFeature) -> Tuple[List[str], List[str]]:
    """(bare and @TEST_ keys: Xray then Jira, @REQ_ keys: Jira only), distinct. Malformed
    prefixed keys are refused here (classify_tag)."""
    xray, jira = [], []
    tags = list(feature.tags)
    for s in feature.scenarios:
        tags += s.rule_tags + s.tags
    for t in tags:
        kind, value = classify_tag(t)
        if kind in ("key", "test") and value not in xray:
            xray.append(value)
        elif kind == "requirement" and value not in jira:
            jira.append(value)
    return xray, [k for k in jira if k not in xray]


_KEY_CHUNK = 50


def lookup_key_tags(ctx: ServerContext, feature: ParsedFeature) -> Dict[str, KeyInfo]:
    """What each issue-key tag names, read before anything is written: Xray tests first (one
    `issue in (...)` read per 50 keys; when Jira refuses the batch, e.g. for a key of no
    project, key by key), then Jira for the rest when it is configured. Without Jira a key
    that is no Xray test is `unknown`."""
    from ..context import is_permission_error
    from ..tools.tests import _search
    xray_keys, jira_only = key_tags_to_look_up(feature)
    info: Dict[str, KeyInfo] = {}

    def read(keys: List[str]) -> List[dict]:
        jql = ("issue = %s" % keys[0]) if len(keys) == 1 else \
            "issue in (%s)" % ", ".join(keys)
        return _search(ctx, jql, len(keys), False)

    for i in range(0, len(xray_keys), _KEY_CHUNK):
        chunk = xray_keys[i:i + _KEY_CHUNK]
        try:
            rows = read(chunk)
        except ToolError as e:
            if is_permission_error(e):
                raise
            rows = []
            for k in (chunk if len(chunk) > 1 else []):
                try:
                    rows += read([k])
                except ToolError as e2:  # e.g. a key of no project: not a test
                    if is_permission_error(e2):
                        raise
        for r in rows:
            k = (r.get("jira") or {}).get("key")
            if k in chunk:
                info[k] = KeyInfo("test")
    jira = ctx.jira
    for k in [k for k in xray_keys if k not in info] + jira_only:
        if jira is None:
            info[k] = KeyInfo("unknown")
            continue
        itype = jira.issue_type(k)
        info[k] = KeyInfo("missing") if itype is None else KeyInfo("issue", itype)
    return info


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
    notes: List[Dict[str, str]] = field(default_factory=list)  # notDone from its key tags
    blocked: str = ""          # why the scenario is not imported (an unusable @TEST_ key)


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
    # issue-key tags: {tag, meaning, note?} per distinct (tag, meaning), first-seen order
    key_tags: List[Dict[str, str]] = field(default_factory=list)
    notes: List[Dict[str, str]] = field(default_factory=list)  # notDone from Feature/Rule tags

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
    info = opts.key_info or {}
    feat = _classify_keys(feature.tags, project_key, info, None, "Feature")
    feat_plain, feat_reqs = feat.plain, feat.reqs
    key_tags: List[Dict[str, str]] = []
    notes: List[Dict[str, str]] = list(feat.notes)

    def add_keys(entries):
        for e in entries:
            if not any(k["tag"] == e["tag"] and k["meaning"] == e["meaning"] for k in key_tags):
                key_tags.append(e)
    add_keys(feat.keys)
    feat_out = _outcome(feat_plain, opts, opts.tags_as_labels)
    plain_all: List[str] = list(feat_plain)
    any_label = _keeps_label(feat_out, opts)
    scenarios: List[ScenarioTags] = []
    seen_keys: Dict[str, int] = {}
    problems: List[str] = []
    for i, s in enumerate(feature.scenarios):
        # Per scenario, not cached by rule name: Rules may share a name or be unnamed.
        rule = _classify_keys(s.rule_tags, project_key, info, None, "Rule")
        r_plain, r_reqs = rule.plain, rule.reqs
        notes.extend(n for n in rule.notes if n not in notes)
        own, test_key = _scenario_tags(s, project_key, info)
        plain, reqs = own.plain, own.reqs
        add_keys(rule.keys + own.keys)
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
        scenarios.append(ScenarioTags(merged, dedupe(feat_reqs + r_reqs + reqs), test_key,
                                      own.notes, "; ".join(own.blocked)))
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
                       uncovered, key_tags, notes)


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


_MEMBERSHIP = ("testSets(limit: 100) { results { issueId } } "
               "preconditions(limit: 100) { results { issueId } } ")


def _ids(results: Any) -> Optional[List[str]]:
    """issueIds of a testSets/preconditions selection; None when it was not returned."""
    if not isinstance(results, dict):
        return None
    return [str(r.get("issueId")) for r in results.get("results") or [] if isinstance(r, dict)]


def existing_test_changes(plan: List[ImportAction], analysis: TagAnalysis,
                          has_jira: bool) -> List[Dict[str, Any]]:
    """What the import would change on each existing test it maps a scenario to: steps,
    labels added, fields, requirement links, Test Sets joined, Pre-Conditions linked, and
    key:value tags still awaiting a tag-rule answer. Only tests with some change are listed.
    Labels, fields and links need Jira; without it they are notDone, not changes. A
    membership that could not be read counts as a change."""
    out: List[Dict[str, Any]] = []
    for a in plan:
        if a.kind != "update_test":
            continue
        st = analysis.scenarios[a.index]
        c: Dict[str, Any] = {"key": a.key, "scenarioIndex": a.index, "scenario": a.summary,
                             "stepsChanged": (a.current_definition or "").strip()
                             != a.definition.strip()}
        if has_jira:
            added = [x for x in a.labels if x not in a.current_labels]
            if added:
                c["labelsAdded"] = added
            if a.field_names:
                c["fields"] = list(a.field_names)
            if a.new_links:
                c["links"] = list(a.new_links)
        sets = [s.summary for s in plan if s.kind in ("create_test_set", "reuse_test_set")
                and a.index in s.tests and (s.kind == "create_test_set"
                                            or a.current_set_ids is None
                                            or s.issue_id not in a.current_set_ids)]
        if sets:
            c["testSets"] = sets
        pres = [p.key or p.summary or "(new)" for p in plan
                if p.kind in ("create_precondition", "reuse_precondition")
                and a.index in p.tests and (p.kind == "create_precondition"
                                            or a.current_pre_ids is None
                                            or p.issue_id not in a.current_pre_ids)]
        if pres:
            c["preconditions"] = pres
        pending = [t for t in st.outcome.unmatched if kv_key(t) in analysis.uncovered]
        if pending:
            c["pendingTags"] = pending
        if c["stepsChanged"] or len(c) > 4:  # more than the four identifying entries
            out.append(c)
    return out


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
        if opts.key_info is None:
            opts = dataclasses.replace(opts, key_info=lookup_key_tags(ctx, feature))
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
        a.not_done = list(st.notes)
        if st.blocked:
            a.kind, a.reason = "skip_test", st.blocked
            a.labels, a.test_sets, a.fields, a.requirement_keys = [], [], {}, []
        elif st.test_key:
            ids = [fid for fid in (_field_id(opts, n) for n in a.fields) if fid]
            r = fetch_test(ctx, project_key, st.test_key, detail=True, jira_fields=ids,
                           xray_extra=_MEMBERSHIP)
            tt = r.get("testType")
            tt = tt.get("name") if isinstance(tt, dict) else tt
            if tt != CUCUMBER:
                raise ToolError("Test %s is a %s test; only Cucumber tests can take a scenario "
                                "definition" % (st.test_key, tt), testKey=st.test_key, testType=tt)
            a.kind, a.key, a.issue_id = "update_test", st.test_key, r["issueId"]
            a.current_definition = r.get("gherkin")
            a.current_set_ids = _ids(r.get("testSets"))
            a.current_pre_ids = _ids(r.get("preconditions"))
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
            elif a.requirement_keys:
                have = set(ctx.jira.linked_keys(a.key))
                a.new_links = [k for k in a.requirement_keys if k not in have]
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
