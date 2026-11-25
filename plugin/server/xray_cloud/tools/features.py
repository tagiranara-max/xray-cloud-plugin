"""create_tests_from_feature: import a .feature file with Xray-native semantics (spec 6.4a)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .. import choices, existingtests, tagquestions
from ..config import ProjectSettings, project_dir
from ..context import (ServerContext, dedupe, is_permission_error, partial_failure,
                       permission_denied, require_project, require_write)
from ..errors import NeedsAnswer, ToolError
from .. import requiredfields
from ..gherkin.parse import ParsedFeature, parse_feature
from ..gherkin.tagrules import FIELD_ID, TagRule, kv_key, parse_rules
from ..jira_client import LINK_TYPE
from ..localfiles import PATH_NOTE, read_local_file
from ..gherkin.plan import (CUCUMBER, UNNAMED_PREFIX, ImportAction, ImportOptions, TagAnalysis,
                            analyze_tags, existing_set_names, existing_test_changes,
                            feature_set_name, lookup_key_tags, plan_import)
from ..gherkin.tagsummary import summarize_tags, tag_values
from ..registry import REGISTRY
from .containers import _CREATE, _add
from .test_sets import SET
from .tests import _CREATE_TEST, _OBJ, _UPDATE_GHERKIN, _schema, check_ai_label

_CREATE_PRECONDITION = """
mutation CreatePrecondition($jira: JSON!, $preconditionType: UpdatePreconditionTypeInput,
                            $definition: String, $testIssueIds: [String]) {
  createPrecondition(jira: $jira, preconditionType: $preconditionType, definition: $definition,
                     testIssueIds: $testIssueIds) {
    precondition { issueId jira(fields: ["key"]) }
    warnings
  }
}
"""
_UPDATE_PRECONDITION = """
mutation UpdatePrecondition($issueId: String!, $data: UpdatePreconditionInput) {
  updatePrecondition(issueId: $issueId, data: $data) { issueId }
}
"""
_ADD_TESTS_TO_PRECONDITION = """
mutation AddTestsToPrecondition($issueId: String!, $testIssueIds: [String]) {
  addTestsToPrecondition(issueId: $issueId, testIssueIds: $testIssueIds) { addedTests warning }
}
"""


def read_feature(env, path: str) -> tuple:
    """-> (text, label path). Relative paths resolve against the project dir
    ($XRAY_PROJECT_DIR, else $CLAUDE_PROJECT_DIR, else cwd; refused when unknown). The label path is relative to the project dir when the
    file is under it, else the basename."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("featureFilePath must be a non-empty path")
    known = project_dir(env)
    full, data = read_local_file(env, path, "Feature file")
    root = os.path.realpath(known) if known is not None else None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ToolError("Feature file is not valid UTF-8: %s (%s)" % (path, e.reason), path=full)
    rel = os.path.basename(full)
    if root is not None and os.path.commonpath([root, full]) == root:
        rel = os.path.relpath(full, root).replace(os.sep, "/")
    return text, rel


def _merge_labels(labels: List[str], extra: Dict[str, Any]) -> List[str]:
    return dedupe(list(labels) + list(extra.pop("labels", None) or []))


def _link_update(req_keys: List[str]) -> List[dict]:
    return [{"add": {"type": {"name": LINK_TYPE}, "outwardIssue": {"key": k}}}
            for k in req_keys]


class _Run:
    """Executes a plan; tracks what was written for partial-failure reports."""

    def __init__(self, ctx: ServerContext, pk: str, plan: List[ImportAction],
                 skip: set, test_extras: Dict[int, dict], pre_extra: dict,
                 set_extra: dict) -> None:
        self.ctx, self.pk, self.plan, self.skip = ctx, pk, plan, skip
        self.test_extras, self.pre_extra, self.set_extra = test_extras, pre_extra, set_extra
        self.created: List[str] = []
        self.updated: List[str] = []
        self.warnings: List[str] = []
        self.step = ""
        self.test_ids: Dict[int, str] = {}   # scenario index -> issueId
        self.test_keys: Dict[int, str] = {}  # scenario index -> key
        self.result: Dict[str, Any] = {}
        self.labels_applied: List[dict] = []
        self.fields_applied: List[dict] = []
        self.sets_created: List[dict] = []
        self.memberships: List[dict] = []

    def gql(self, query: str, variables: dict) -> dict:
        return self.ctx.xray.graphql(query, variables)

    # -- tests
    def create_test(self, a: ImportAction) -> None:
        extra = dict(self.test_extras[a.index])
        fields: Dict[str, Any] = {"project": {"key": self.pk}, "summary": a.summary}
        if a.description:
            fields["description"] = a.description
            extra.pop("description", None)  # the feature's own text wins
        labels = _merge_labels(a.labels, extra)
        fields.update(extra)
        if labels:
            fields["labels"] = labels
        jira: Dict[str, Any] = {"fields": fields}
        if a.requirement_keys:
            jira["update"] = {"issuelinks": _link_update(a.requirement_keys)}
        with requiredfields.creating(self.ctx, self.pk, "test"):
            data = self.gql(_CREATE_TEST, {"jira": jira, "testType": {"name": CUCUMBER},
                                           "gherkin": a.definition})
        res = data.get("createTest") or {}
        test = res.get("test") or {}
        key = (test.get("jira") or {}).get("key")
        if not key or not test.get("issueId"):
            raise ToolError("Xray did not return a key for the created test %r" % a.summary)
        self.created.append(key)
        self.test_ids[a.index], self.test_keys[a.index] = test["issueId"], key
        self.warnings.extend(res.get("warnings") or [])

    def update_test(self, a: ImportAction) -> None:
        """Existing test: new definition (Xray). Labels, empty rule fields and requirement
        links need Jira; without it they are in the plan's notDone and skipped here."""
        self.ctx.step_cache.invalidate(a.key)
        self.gql(_UPDATE_GHERKIN, {"issueId": a.issue_id, "gherkin": a.definition})
        self.updated.append(a.key)
        self.test_ids[a.index], self.test_keys[a.index] = a.issue_id, a.key
        jira = self.ctx.jira
        if jira is None:
            return
        new_labels = [l for l in a.labels if l not in a.current_labels]
        edit: Dict[str, Any] = {}
        if new_labels:
            edit["labels"] = a.current_labels + new_labels
        edit.update(a.field_updates)
        if edit:
            jira.edit_issue(a.key, edit)
            if new_labels:
                self.labels_applied.append({"test": a.key, "added": list(new_labels)})
            if a.field_updates:
                self.fields_applied.append({"test": a.key, "fields": list(a.field_names)})
        if a.requirement_keys:
            have = set(jira.linked_keys(a.key))
            todo = [k for k in a.requirement_keys if k not in have]
            entry = None
            for k in todo:
                jira.link_requirement(a.key, k)
                if entry is None:  # recorded as each link is made, for partial-failure reports
                    entry = {"test": a.key, "requirements": []}
                    self.result.setdefault("linkedRequirements", []).append(entry)
                entry["requirements"].append(k)

    # -- Pre-Conditions
    def precondition(self, a: ImportAction) -> None:
        ids = [self.test_ids[i] for i in a.tests if i in self.test_ids]
        keys = [self.test_keys[i] for i in a.tests if i in self.test_keys]
        if not ids:
            self.result.setdefault("skippedPreconditions", []).append(
                {"summary": a.summary, "line": a.source_line,
                 "reason": "none of its scenarios was imported"})
            return
        summary = UNNAMED_PREFIX + ", ".join(keys) if a.unnamed else a.summary
        if a.kind == "create_precondition":
            extra = dict(self.pre_extra)
            fields: Dict[str, Any] = {"project": {"key": self.pk}, "summary": summary}
            if a.description:
                fields["description"] = a.description
                extra.pop("description", None)  # the Background's own text wins
            labels = _merge_labels(a.labels, extra)
            fields.update(extra)
            fields["labels"] = labels
            with requiredfields.creating(self.ctx, self.pk, "precondition",
                                         "preconditionFields"):
                data = self.gql(_CREATE_PRECONDITION, {
                    "jira": {"fields": fields}, "preconditionType": {"name": CUCUMBER},
                    "definition": a.definition, "testIssueIds": ids})
            res = data.get("createPrecondition") or {}
            key = ((res.get("precondition") or {}).get("jira") or {}).get("key")
            if not key:
                raise ToolError("Xray did not return a key for the created Pre-Condition %r"
                                % summary)
            self.created.append(key)
            self.warnings.extend(res.get("warnings") or [])
            out_key = key
        else:
            out_key = a.key
            if a.update_definition:
                self.gql(_UPDATE_PRECONDITION, {"issueId": a.issue_id,
                                                "data": {"definition": a.definition}})
                self.updated.append(a.key)
            data = self.gql(_ADD_TESTS_TO_PRECONDITION, {"issueId": a.issue_id,
                                                         "testIssueIds": ids})
            w = (data.get("addTestsToPrecondition") or {}).get("warning")
            if w:
                self.warnings.append(w)
            summary = a.current_summary or a.summary  # reuse: summary left as it is
        self.result.setdefault("preconditions", []).append(
            {"key": out_key, "summary": summary,
             "created": a.kind == "create_precondition", "tests": keys})

    # -- Test Sets
    def test_set(self, a: ImportAction) -> None:
        ids = [self.test_ids[i] for i in a.tests if i in self.test_ids]
        keys = [self.test_keys[i] for i in a.tests if i in self.test_keys]
        if not ids:
            skipped = {"skipped": True, "reason": "no tests were imported"}
            if a.feature_set:
                self.result["testSet"] = skipped
            else:
                self.result.setdefault("skippedTestSets", []).append(
                    dict(name=a.summary, **skipped))
            return
        if a.kind == "create_test_set":
            extra = dict(self.set_extra)
            fields: Dict[str, Any] = {"project": {"key": self.pk}, "summary": a.summary}
            if a.description:
                fields["description"] = a.description
                extra.pop("description", None)  # the Feature's own text wins
            labels = _merge_labels(a.labels, extra)
            fields.update(extra)
            if labels:
                fields["labels"] = labels
            with requiredfields.creating(self.ctx, self.pk, "testSet", "testSetFields"):
                data = self.gql(SET.q(_CREATE), {"jira": {"fields": fields}})
            node = (data.get("createTestSet") or {}).get("testSet") or {}
            key = (node.get("jira") or {}).get("key")
            if not key:
                raise ToolError("Xray did not return a key for the created Test Set %r"
                                % a.summary)
            self.created.append(key)
            self.sets_created.append({"key": key, "name": a.summary})
            issue_id = node["issueId"]
        else:
            key, issue_id = a.key, a.issue_id
        self.step = "adding tests to Test Set %s" % key
        res = _add(self.ctx, SET, issue_id, ids)
        self.warnings.extend(res["warnings"])
        self.memberships.append({"testSet": key, "name": a.summary, "tests": keys})
        if a.feature_set:
            self.result["testSet"] = {"key": key, "summary": a.summary,
                                      "created": a.kind == "create_test_set",
                                      "addedTests": len(ids)}

    def run(self) -> Dict[str, Any]:
        try:
            for a in self.plan:
                if a.kind in ("create_test", "update_test"):
                    if a.index in self.skip:
                        continue
                    self.step = "%s %r (line %d)" % (
                        "creating test" if a.kind == "create_test" else "updating test " + a.key,
                        a.summary, a.source_line)
                    (self.create_test if a.kind == "create_test" else self.update_test)(a)
                elif a.kind in ("create_precondition", "reuse_precondition"):
                    self.step = "%s Pre-Condition %r (line %d)" % (
                        "creating" if a.kind == "create_precondition" else "reusing " + a.key,
                        a.summary, a.source_line)
                    self.precondition(a)
                elif a.kind in ("create_test_set", "reuse_test_set"):
                    self.step = "%s Test Set %r" % (
                        "creating" if a.kind == "create_test_set" else "reusing " + a.key,
                        a.summary)
                    self.test_set(a)
        except Exception as e:
            if not self.created and not self.updated:
                # registered read-only (for the preview), so the registry does not map this
                if isinstance(e, ToolError) and is_permission_error(e):
                    raise permission_denied(e, action="run create_tests_from_feature",
                                            project_key=self.pk) from None
                raise
            msg = ("Feature import stopped while %s; the issues listed were already written"
                   % self.step)
            err = (requiredfields.refusal_failure(msg, self.created, e)
                   if requiredfields.is_refusal(e) else partial_failure(msg, self.created, e))
            err.data["updated"] = list(self.updated)
            err.data["failedStep"] = self.step
            if self.result.get("linkedRequirements"):
                err.data["linkedRequirements"] = list(self.result["linkedRequirements"])
            if self.labels_applied:
                err.data["labelsApplied"] = list(self.labels_applied)
            if self.fields_applied:
                err.data["fieldsApplied"] = list(self.fields_applied)
            if self.sets_created:
                err.data["testSetsCreated"] = list(self.sets_created)
            if self.memberships:
                err.data["testSetMemberships"] = list(self.memberships)
            raise err
        return self.result


def _skipped(plan: List[ImportAction], create_dups: List[int]) -> List[dict]:
    return [{"index": a.index, "summary": a.summary, "line": a.source_line,
             "candidates": a.duplicates}
            for a in plan if a.kind == "create_test" and a.duplicates
            and a.index not in create_dups]


def _apply_existing_answers(plan: List[ImportAction], changes: List[dict],
                            answers: Dict[str, Any]) -> None:
    """Existing tests: one with nothing to change is only mapped (keep_test); otherwise the
    answer decides: update (as planned), keep (mapped, nothing written to it) or skip (the
    scenario is left out). Unanswered (preview): left as planned."""
    changed = {c["key"] for c in changes}
    for a in plan:
        if a.kind != "update_test":
            continue
        ans = existingtests.answer_for(answers, a.key) if a.key in changed else "unchanged"
        if ans in ("keep", "unchanged"):
            a.kind = "keep_test"
            if ans == "keep":
                a.not_done = []
        elif ans == "skip":
            a.kind, a.not_done = "skip_test", []
            a.reason = "existing test %s left as it is and the scenario left out " \
                       "(existingTests: skip)" % a.key


QUESTION_IDS = ("createTestSet", "backgrounds", "tagsAsLabels", "aiLabel", "aiLabelScope")
_BOOL = {"type": "boolean"}


def _rules(ctx: ServerContext, pk: str, project: ProjectSettings) -> Tuple[List[TagRule], Any]:
    """Resolved tag rules for the project: settings rules (parsed already) first, then the
    remembered ones (raw JSON, parsed here; answers to the import's tag questions land there).
    A settings rule naming a field this project lacks is refused (top-level rules are checked
    against the union of all projects at load)."""
    value, source = choices.resolve(ctx, pk, "tagRules", None)
    names = set((project.fields.get("test") or {}))
    if value is None:
        return [], None
    if source != "settings":
        return parse_rules(value, names, "remembered tagRules of %s" % pk), source
    for i, r in enumerate(value):
        unknown = sorted(n for n in r.fields if n not in names and not FIELD_ID.fullmatch(n))
        if unknown:
            raise ToolError("featureImport.tagRules[%d] sets field(s) %s, which project %s "
                            "does not define in fields.test; nothing was imported"
                            % (i, ", ".join(unknown), pk), unknownFields=unknown)
    rules = list(value)
    remembered = choices.remembered_value(ctx, pk, "tagRules")
    if remembered:
        rules += parse_rules(remembered, names, "remembered tagRules of %s" % pk)
        source = "settings+remembered"
    return rules, source


def _applicable(feature: ParsedFeature, analysis: TagAnalysis, values: Dict[str, Any]) -> Dict[str, bool]:
    """Which questions apply (spec 8). Decided without any call so preview and apply agree;
    creation is counted as possible when it may happen (duplicates and reuse are not known)."""
    named = bool(feature.name.strip())
    cts = values["createTestSet"]
    new_tests = any(st.test_key is None and not st.blocked for st in analysis.scenarios)
    new_pre = bool(feature.backgrounds) and values["backgrounds"] != "inline"
    new_sets = (named and cts is not False) or bool(analysis.tag_sets)
    ai = new_tests or new_pre or new_sets
    return {"createTestSet": named,
            "backgrounds": bool(feature.backgrounds),
            "tagsAsLabels": analysis.any_label,
            "aiLabel": ai,
            # asked with an unanswered aiLabel too, so one round of answers is enough
            "aiLabelScope": ai and values["aiLabel"] != "" and (new_pre or new_sets)}


def _contexts(feature: ParsedFeature, analysis: TagAnalysis, rel: str,
              plan: Optional[List[ImportAction]], skip: set) -> Dict[str, dict]:
    """Facts for each question. Without a plan (the apply refusal for unanswered choices)
    only file facts."""
    ctx_set: Dict[str, Any] = {"testSetName": feature_set_name(feature, rel)}
    out = {"createTestSet": ctx_set,
           "backgrounds": {"backgrounds": [
               dict({"name": b.name, "scope": b.scope, "line": b.line, "steps": len(b.steps)},
                    **({"rule": b.rule} if b.rule else {})) for b in feature.backgrounds]},
           "tagsAsLabels": {"tags": [t for t in analysis.plain_tags if kv_key(t) is None]},
           "aiLabel": {}, "aiLabelScope": {}}
    if plan is not None:
        fs = next((a for a in plan if a.feature_set), None)
        if fs is not None:
            ctx_set["exists"] = fs.kind == "reuse_test_set"
            if fs.key:
                ctx_set["key"] = fs.key
        counts = _would_create(plan, skip)
        out["aiLabel"]["wouldCreate"] = counts
        out["aiLabelScope"]["wouldCreate"] = counts
    return out


def _would_create(plan: List[ImportAction], skip: set) -> Dict[str, int]:
    imported = {a.index for a in plan if a.kind in ("create_test", "update_test")
                and a.index not in skip}
    def n(kind):
        return sum(1 for a in plan if a.kind == kind
                   and (kind == "create_test" and a.index in imported
                        or kind != "create_test" and any(i in imported for i in a.tests)))
    return {"tests": n("create_test"), "preconditions": n("create_precondition"),
            "testSets": n("create_test_set")}


def _members(plan: List[ImportAction], indexes: List[int]) -> List[str]:
    by_index = {a.index: a for a in plan
                if a.kind in ("create_test", "update_test", "skip_test", "keep_test")}
    return [by_index[i].key or by_index[i].summary for i in indexes]


def _preview_sets(plan: List[ImportAction]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {"reuse": [], "create": []}
    for a in plan:
        if a.kind == "reuse_test_set":
            out["reuse"].append({"name": a.summary, "key": a.key,
                                 "members": _members(plan, a.tests)})
        elif a.kind == "create_test_set":
            out["create"].append({"name": a.summary, "members": _members(plan, a.tests)})
    return out


def _per_test(plan: List[ImportAction]) -> List[dict]:
    out = []
    for a in plan:
        if a.kind in ("create_test", "update_test", "keep_test"):
            row = {"scenarioIndex": a.index, "scenario": a.summary}
            if a.key:
                row["key"] = a.key
            row.update(labels=list(a.labels), testSets=list(a.test_sets), fields=dict(a.fields))
            out.append(row)
    return out


def _own_description(items) -> List[str]:
    """["description"] when every item (with a .description) has its own, else []."""
    items = list(items)
    return ["description"] if items and all(getattr(x, "description", "") for x in items) \
        else []


def _test_extras(c: requiredfields.Collector, feature: ParsedFeature, analysis: TagAnalysis,
                 args: dict) -> Dict[int, dict]:
    """Jira fields per new test: the call's fields plus the rule fields of that test. A rule
    value that differs from the call's value for the same field refuses the file. Missing
    required fields are collected only for scenarios that create a test, with their rule
    fields."""
    call_fields = dict(args.get("fields") or {})
    base_users = [i for i, st in enumerate(analysis.scenarios)
                  if st.test_key is None and not st.blocked and not st.outcome.fields]
    # description is not asked for tests whose scenario has its own
    base = c("test", call_fields, args.get("rawFields"), required=bool(base_users),
             provided=_own_description(feature.scenarios[i] for i in base_users))
    out: Dict[int, dict] = {}
    for i, st in enumerate(analysis.scenarios):
        rule_fields = st.outcome.fields
        if st.test_key is not None or st.blocked or not rule_fields:
            out[i] = base
            continue
        clash = {n: v for n, v in rule_fields.items() if n in call_fields and call_fields[n] != v}
        if clash:
            name = next(iter(clash))
            raise ToolError("Field %s is %r in the call's fields but %r from %s on scenario %r "
                            "(line %d); nothing was imported"
                            % (name, call_fields[name], clash[name],
                               st.outcome.sources.get(name, "a tag rule"),
                               feature.scenarios[i].name, feature.scenarios[i].line),
                            field=name)
        out[i] = c("test", dict(call_fields, **rule_fields), args.get("rawFields"),
                   record=False,  # a rule value is never remembered
                   provided=_own_description([feature.scenarios[i]]))
    return out


@REGISTRY.tool(
    "create_tests_from_feature",
    "Import a .feature file: Scenario -> Cucumber test, Background -> Pre-Condition (or "
    "inline), Feature -> Test Set. Tags go through the project's tag rules into labels, Test "
    "Sets and fields. Issue-key tags are looked up: @<KEY> of a test in this project maps the "
    "scenario to it, of another issue links it, of no issue is a plain tag (tagSummary."
    "keyTags); @REQ_<KEY> links. Unanswered choices (createTestSet, backgrounds, tagsAsLabels, "
    "aiLabel, aiLabelScope, tagRule.<key> in tagRuleAnswers, existingTests for tests it would "
    "change) are in a non-empty `questions`: ask the user "
    "once, call again with the answers. apply=false (default) previews; apply=true executes, "
    "skipping scenarios whose summary an existing test has unless indexed in "
    "createDuplicates (`similar` ones are only listed). Anything not doable is in notDone "
    "{item, what, reason, fix}: if Jira access is missing and Atlassian MCP tools are "
    "available, offer to complete them with those. fields/rawFields apply to created tests.",
    _schema({"featureFilePath": {"type": "string", "description":
                                 "Local .feature file. " + PATH_NOTE},
             "apply": {"type": "boolean"},
             "createTestSet": _BOOL,
             "backgrounds": {"type": "string", "enum": ["preconditions", "inline"]},
             "tagsAsLabels": _BOOL,
             "aiLabel": {"type": "string", "description":
                         "The user's own label for items the AI creates; \"\" = don't mark."},
             "aiLabelScope": {"type": "string", "enum": ["tests", "all"], "description":
                              "tests | all. all also marks new Pre-Conditions and Test Sets."},
             "remember": {"type": "boolean", "description":
                          "Answers are remembered for the project on apply when nothing is "
                          "remembered yet. Default false: a differing answer applies to this "
                          "call only; true makes it replace the remembered one."},
             "tagRuleAnswers": tagquestions.PARAM_SCHEMA,
             **existingtests.PARAM_SCHEMAS,
             "createDuplicates": {"type": "array", "items": {"type": "integer", "minimum": 0}},
             "fields": _OBJ, "rawFields": _OBJ,
             "preconditionFields": {"type": "object", "description":
                                    "Field values applied to created Pre-Conditions."},
             "testSetFields": {"type": "object", "description":
                               "Field values applied to created Test Sets."}},
            ["featureFilePath"]), approval=True)
# Registered read-only so the preview (apply=false, zero writes) works in read-only mode;
# apply=true checks require_write itself.
def create_tests_from_feature(ctx, args):
    pk = args["projectKey"]
    apply = args.get("apply", False)
    if apply:
        require_write(ctx)
    project = require_project(ctx, pk, write=apply)
    check_ai_label(args.get("aiLabel"))
    text, rel = read_feature(ctx.env, args["featureFilePath"])
    feature = parse_feature(text, rel)
    if not feature.scenarios:
        raise ToolError("No scenarios found in %s" % rel, path=rel)

    # -- choices: call -> settings -> remembered -> unanswered (no calls so far)
    resolved = {n: choices.resolve(ctx, pk, n, args.get(n)) for n in QUESTION_IDS}
    resolved["unmatchedTags"] = choices.resolve(ctx, pk, "unmatchedTags", None)
    values = {n: v for n, (v, _) in resolved.items()}
    rules, rules_source = _rules(ctx, pk, project)
    opts = ImportOptions(
        # spec 8: never a Feature Test Set without a Feature name (whatever settings or
        # remembered answers say); unanswered with a name: preview as "yes"
        create_test_set=bool(feature.name.strip()) and values["createTestSet"] is not False,
        backgrounds=values["backgrounds"] or "preconditions",
        tags_as_labels=values["tagsAsLabels"] is not False,
        unmatched_tags=values["unmatchedTags"] or "keep",
        rules=rules, ai_label=values["aiLabel"] or "",
        ai_scope=values["aiLabelScope"] or "tests", project=project)
    # key:value tags no rule covers: answered rules are added (remembered on apply)
    tag_answers = tagquestions.parse_answers(args.get(tagquestions.PARAM))
    exist_answers = existingtests.parse(args)
    names = set(project.fields.get("test") or {})
    new_rules: List[dict] = []

    def analyze():
        opts.rules = rules + parse_rules(new_rules, names, tagquestions.PARAM)
        return analyze_tags(feature, pk, opts)

    # what issue-key tags name: read before anything else is decided (nothing is written)
    opts.key_info = lookup_key_tags(ctx, feature)
    analysis = analyze_tags(feature, pk, opts)
    new_rules = tagquestions.pending(tag_answers, analysis.uncovered, project)
    if new_rules:
        analysis = analyze()
    applicable = _applicable(feature, analysis, values)
    unanswered = [n for n in QUESTION_IDS if applicable[n] and values[n] is None]
    def make_plan():
        """(plan, changes to existing tests). Planned this early (it only reads) only when a
        scenario maps to an existing test, so that question joins the tag ones; otherwise
        after the field checks, which refuse before any call."""
        if not any(st.test_key for st in analysis.scenarios):
            return None, []
        if not applicable["aiLabel"]:
            opts.ai_label = ""
        p = plan_import(ctx, pk, feature, rel, opts, analysis)
        return p, existing_test_changes(p, analysis, ctx.jira is not None)

    plan, changes = make_plan()
    ask_changes = existingtests.unanswered(changes, exist_answers)
    if apply and unanswered:  # every open question in one round; nothing was written
        contexts = _contexts(feature, analysis, rel, None, set())
        raise ToolError("Answer these first: %s (ask the user, then call again with the "
                        "answers; nothing was written)"
                        % ", ".join(unanswered + ["tagRule.%s" % k for k in analysis.uncovered]
                                    + ([existingtests.QUESTION_ID] if ask_changes else [])),
                        questions=[choices.question(n, contexts[n]) for n in unanswered]
                        + tagquestions.questions(analysis.uncovered, project)
                        + ([existingtests.question(ask_changes)] if ask_changes else []))
    if apply and (analysis.uncovered or ask_changes):  # elicitation form, else needs_answer
        asked, ex = tagquestions.ask(ctx, analysis.uncovered, project, ask_changes)
        if ex is not None:
            exist_answers = {"all": exist_answers["all"] or ex["all"],
                             "keys": dict(ex["keys"], **exist_answers["keys"])}
        if analysis.uncovered:
            new_rules = new_rules + tagquestions.pending(asked, analysis.uncovered, project)
            analysis = analyze()
            applicable = _applicable(feature, analysis, values)
            plan, changes = make_plan()
        left = existingtests.unanswered(changes, exist_answers)
        if left:
            raise NeedsAnswer("Ask the user whether to update, keep or skip these existing "
                              "tests, then call again with existingTests; never answer it "
                              "yourself. Nothing was written.",
                              [existingtests.question(left)], projectKey=pk)
    if not applicable["aiLabel"]:
        opts.ai_label = ""

    # -- fields for every item type that may be created: refuse the whole file first
    def build(c):
        tests = _test_extras(c, feature, analysis, args)
        pre = (c("precondition", args.get("preconditionFields"), None, "preconditionFields",
                 provided=_own_description(feature.backgrounds))
               if feature.backgrounds and not opts.inline else {})
        # tag Test Sets have no description; the Feature Test Set has the Feature's
        set_items = ([feature] if opts.create_test_set else []) + \
            ([None] if analysis.tag_sets else [])
        sets = (c("testSet", args.get("testSetFields"), None, "testSetFields",
                  provided=_own_description(set_items))
                if opts.create_test_set or analysis.tag_sets else {})
        return tests, pre, sets

    field_questions: List[dict] = []
    if apply:  # every missing field of every item type is asked at once
        (test_extras, pre_extra, set_extra), applied_defaults = requiredfields.resolve_all(
            ctx, project, build, bool(args.get("remember", False)))
    else:  # the preview never asks: the questions are listed
        collector = requiredfields.Collector(ctx, project)
        test_extras, pre_extra, set_extra = build(collector)
        field_questions = collector.questions()
    create_dups = list(args.get("createDuplicates") or [])
    bad = [i for i in create_dups if i >= len(feature.scenarios)]
    if bad:
        raise ToolError("createDuplicates index out of range: %s (the feature has %d scenarios)"
                        % (bad, len(feature.scenarios)))
    if plan is None:
        plan = plan_import(ctx, pk, feature, rel, opts, analysis)
    _apply_existing_answers(plan, changes, exist_answers)
    skipped = _skipped(plan, create_dups)
    skip = {s["index"] for s in skipped}
    not_done = list(analysis.notes) + [nd for a in plan if a.index not in skip
                                       for nd in a.not_done]
    if not apply:
        contexts = _contexts(feature, analysis, rel, plan, skip)
        shown = {n: {"value": v, "source": src} for n, (v, src) in resolved.items()}
        for n in QUESTION_IDS:
            if not applicable[n]:
                shown[n]["notApplicable"] = True
        shown["tagRules"] = {"count": len(opts.rules), "source": rules_source}
        out: Dict[str, Any] = {"preview": True, "feature": feature.name, "path": rel,
                               "questions": [choices.question(n, contexts[n])
                                             for n in unanswered]
                               + tagquestions.questions(analysis.uncovered, project)
                               + ([existingtests.question(ask_changes)] if ask_changes else [])
                               + field_questions,
                               "choices": shown,
                               "actions": [a.to_dict() for a in plan],
                               "perTest": _per_test(plan), "testSets": _preview_sets(plan),
                               "tagSummary": summarize_tags(
                                   [feature], opts.rules,
                                   existing_set_names(ctx, pk, tag_values([feature])),
                                   analysis.key_tags),
                               "notDone": not_done}
        if analysis.warnings:
            out["warnings"] = list(analysis.warnings)
        if changes:
            out["existingTests"] = changes
        if unanswered or analysis.uncovered or ask_changes:
            out["hint"] = ("answer the questions (ask the user once), then call again with the "
                           "answers (tagRule.<key> ones in tagRuleAnswers, existingTests in "
                           "existingTests / existingTestOverrides); until then the "
                           "actions assume createTestSet=true, backgrounds=preconditions, "
                           "tagsAsLabels=true, no AI label and nothing from unanswered "
                           "key:value tags")
        elif field_questions:
            out["hint"] = ("required fields have no value: ask the user, then call again with "
                           "each answer in the question's `param` ({field id: value})")
        elif skipped:
            out["hint"] = ("scenarios with possible duplicates are skipped on apply unless "
                           "their scenarioIndex is in createDuplicates; pass apply: true to import")
        else:
            out["hint"] = "pass apply: true to import"
        if skipped:
            out["possibleDuplicates"] = skipped
        return out
    # All validation passed: remember the call's answers, then write (ruling R5).
    choices.remember(ctx, pk, {n: args.get(n) for n in QUESTION_IDS},
                     bool(args.get("remember", False)))
    choices.add_tag_rules(ctx, pk, new_rules)
    run = _Run(ctx, pk, plan, skip, test_extras, pre_extra, set_extra)
    result = run.run()
    kept = {a.index: a.key for a in plan if a.kind == "keep_test"}
    mapped = {**run.test_keys, **kept}
    out = {"feature": feature.name, "path": rel, "created": run.created, "updated": run.updated,
           "tests": [{"index": i, "key": mapped[i]} for i in sorted(mapped)]}
    if kept:
        out["unchanged"] = [kept[i] for i in sorted(kept)]
    out.update(result)
    out["testSetsCreated"] = run.sets_created
    out["testSetMemberships"] = run.memberships
    if run.fields_applied:
        out["fieldsApplied"] = run.fields_applied
    for item_type, applied in applied_defaults.items():
        requiredfields.add_applied(out, item_type, applied)
    out["notDone"] = not_done
    if skipped:
        out["skippedDuplicates"] = skipped
        out["hint"] = "pass the skipped indexes in createDuplicates to create them anyway"
    warnings = list(analysis.warnings) + run.warnings
    if warnings:
        out["warnings"] = warnings
    return out
