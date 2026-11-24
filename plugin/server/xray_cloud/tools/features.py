"""create_tests_from_feature: import a .feature file with Xray-native semantics (spec 6.4a)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .. import choices
from ..config import ProjectSettings, project_dir
from ..context import (ServerContext, dedupe, is_permission_error, partial_failure,
                       permission_denied, require_project, require_write)
from ..errors import ToolError
from .. import requiredfields
from ..gherkin.parse import ParsedFeature, parse_feature
from ..gherkin.tagrules import TagRule, parse_rules
from ..jira_client import LINK_TYPE
from ..localfiles import read_local_file
from ..gherkin.plan import (CUCUMBER, UNNAMED_PREFIX, ImportAction, ImportOptions, TagAnalysis,
                            analyze_tags, existing_set_names, feature_set_name, plan_import)
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
    ($XRAY_PROJECT_DIR, else $CLAUDE_PROJECT_DIR, else cwd). The label path is relative to the project dir when the
    file is under it, else the basename."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("featureFilePath must be a non-empty path")
    root = os.path.realpath(project_dir(env))
    full, data = read_local_file(env, path, "Feature file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ToolError("Feature file is not valid UTF-8: %s (%s)" % (path, e.reason), path=full)
    rel = os.path.basename(full)
    if os.path.commonpath([root, full]) == root:
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
        labels = _merge_labels(a.labels, extra)
        fields.update(extra)
        if labels:
            fields["labels"] = labels
        jira: Dict[str, Any] = {"fields": fields}
        if a.requirement_keys:
            jira["update"] = {"issuelinks": _link_update(a.requirement_keys)}
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
            labels = _merge_labels(a.labels, extra)
            fields.update(extra)
            fields["labels"] = labels
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
            labels = _merge_labels(a.labels, extra)
            fields.update(extra)
            if labels:
                fields["labels"] = labels
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
            err = partial_failure("Feature import stopped while %s; the issues listed were "
                                  "already written" % self.step, self.created, e)
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


QUESTION_IDS = ("createTestSet", "backgrounds", "tagsAsLabels", "aiLabel", "aiLabelScope")
_BOOL = {"type": "boolean"}


def _rules(ctx: ServerContext, pk: str, project: ProjectSettings) -> Tuple[List[TagRule], Any]:
    """Resolved tag rules for the project (settings rules are parsed already; remembered ones
    are raw JSON and parsed here). A settings rule naming a field this project lacks is refused
    (top-level rules are checked against the union of all projects at load)."""
    value, source = choices.resolve(ctx, pk, "tagRules", None)
    names = set((project.fields.get("test") or {}))
    if value is None:
        return [], None
    if all(isinstance(r, TagRule) for r in value):
        for i, r in enumerate(value):
            unknown = sorted(n for n in r.fields if n not in names)
            if unknown:
                raise ToolError("featureImport.tagRules[%d] sets field(s) %s, which project %s "
                                "does not define in fields.test; nothing was imported"
                                % (i, ", ".join(unknown), pk), unknownFields=unknown)
        return list(value), source
    return parse_rules(value, names, "remembered tagRules of %s" % pk), source


def _applicable(feature: ParsedFeature, analysis: TagAnalysis, values: Dict[str, Any]) -> Dict[str, bool]:
    """Which questions apply (spec 8). Decided without any call so preview and apply agree;
    creation is counted as possible when it may happen (duplicates and reuse are not known)."""
    named = bool(feature.name.strip())
    cts = values["createTestSet"]
    new_tests = any(st.test_key is None for st in analysis.scenarios)
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
    """Facts for each question. Without a plan (apply refusal: no calls) only file facts."""
    ctx_set: Dict[str, Any] = {"testSetName": feature_set_name(feature, rel)}
    out = {"createTestSet": ctx_set,
           "backgrounds": {"backgrounds": [
               dict({"name": b.name, "scope": b.scope, "line": b.line, "steps": len(b.steps)},
                    **({"rule": b.rule} if b.rule else {})) for b in feature.backgrounds]},
           "tagsAsLabels": {"tags": list(analysis.plain_tags)},
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
    by_index = {a.index: a for a in plan if a.kind in ("create_test", "update_test")}
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
        if a.kind in ("create_test", "update_test"):
            row = {"scenarioIndex": a.index, "scenario": a.summary}
            if a.key:
                row["key"] = a.key
            row.update(labels=list(a.labels), testSets=list(a.test_sets), fields=dict(a.fields))
            out.append(row)
    return out


class _Fields:
    """Required-field resolution for one import: asks on apply (requiredfields.resolve);
    the preview only collects the questions."""

    def __init__(self, ctx, project: ProjectSettings, apply: bool, remember: bool) -> None:
        self.ctx, self.project, self.apply, self.remember = ctx, project, apply, remember
        self.questions: List[dict] = []
        self.applied: Dict[str, Dict[str, Any]] = {}
        self._asked = set()

    def __call__(self, item_type: str, fields, raw_fields, param: str = "fields",
                 record: bool = True) -> dict:
        if self.apply:
            out, applied = requiredfields.resolve(self.ctx, self.project, item_type, fields,
                                                  raw_fields, self.remember, param=param,
                                                  record=record)
        else:
            r = requiredfields.compute(self.ctx, self.project, item_type, fields, raw_fields)
            out, applied = r.fields, r.applied
            new = [f for f in r.missing if (item_type, f["id"]) not in self._asked]
            self._asked.update((item_type, f["id"]) for f in new)
            self.questions.extend(requiredfields.questions(self.project, item_type, new, param))
        if applied:
            self.applied.setdefault(item_type, {}).update(applied)
        return out


def _test_extras(resolve: _Fields, feature: ParsedFeature, analysis: TagAnalysis,
                 args: dict) -> Dict[int, dict]:
    """Jira fields per new test: the call's fields plus the rule fields of that test. A rule
    value that differs from the call's value for the same field refuses the file."""
    call_fields = dict(args.get("fields") or {})
    base = resolve("test", call_fields, args.get("rawFields"))
    out: Dict[int, dict] = {}
    for i, st in enumerate(analysis.scenarios):
        rule_fields = st.outcome.fields
        if st.test_key is not None or not rule_fields:
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
        out[i] = resolve("test", dict(call_fields, **rule_fields), args.get("rawFields"),
                         record=False)  # a rule value is never remembered
    return out


@REGISTRY.tool(
    "create_tests_from_feature",
    "Import a .feature file the Xray way: each Scenario/Outline becomes a Cucumber test. "
    "Backgrounds become Pre-Conditions (or inline steps with backgrounds=inline); the Feature"
    " can become a Test Set. Tags go through the project's tag rules (settings or remembered;"
    " none built in) into labels, Test Sets and fields; @TEST_<KEY> or @<KEY> updates that "
    "test, @REQ_<KEY> links a requirement. Choices (createTestSet, backgrounds, tagsAsLabels,"
    " aiLabel, aiLabelScope) come from the call, settings or remembered answers. If the "
    "preview has non-empty `questions`, ask the user once and call again with the answers; "
    "apply=true with unanswered questions is refused. apply=false (default) previews the "
    "plan; apply=true executes it, skipping possible duplicates unless indexed in "
    "createDuplicates. Anything not doable is in notDone {item, what, reason, fix}: if the "
    "reason is \"no Jira token or Atlassian MCP\" and Atlassian MCP tools are available, offer "
    "to complete them with those. fields/rawFields apply to created tests.",
    _schema({"featureFilePath": {"type": "string"}, "apply": {"type": "boolean"},
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
    analysis = analyze_tags(feature, pk, opts)
    applicable = _applicable(feature, analysis, values)
    if not applicable["aiLabel"]:
        opts.ai_label = ""
    unanswered = [n for n in QUESTION_IDS if applicable[n] and values[n] is None]
    if apply and unanswered:
        contexts = _contexts(feature, analysis, rel, None, set())
        raise ToolError("Answer these first: %s (ask the user, then call again with the "
                        "answers; nothing was written)" % ", ".join(unanswered),
                        questions=[choices.question(n, contexts[n]) for n in unanswered])

    # -- fields for every item type that may be created: refuse the whole file first
    resolve = _Fields(ctx, project, apply, bool(args.get("remember", False)))
    test_extras = _test_extras(resolve, feature, analysis, args)
    pre_extra = (resolve("precondition", args.get("preconditionFields"), None,
                         "preconditionFields")
                 if feature.backgrounds and not opts.inline else {})
    set_extra = (resolve("testSet", args.get("testSetFields"), None, "testSetFields")
                 if opts.create_test_set or analysis.tag_sets else {})
    create_dups = list(args.get("createDuplicates") or [])
    bad = [i for i in create_dups if i >= len(feature.scenarios)]
    if bad:
        raise ToolError("createDuplicates index out of range: %s (the feature has %d scenarios)"
                        % (bad, len(feature.scenarios)))
    plan = plan_import(ctx, pk, feature, rel, opts, analysis)
    skipped = _skipped(plan, create_dups)
    skip = {s["index"] for s in skipped}
    not_done = [nd for a in plan if a.index not in skip for nd in a.not_done]
    if not apply:
        contexts = _contexts(feature, analysis, rel, plan, skip)
        shown = {n: {"value": v, "source": src} for n, (v, src) in resolved.items()}
        for n in QUESTION_IDS:
            if not applicable[n]:
                shown[n]["notApplicable"] = True
        shown["tagRules"] = {"count": len(rules), "source": rules_source}
        out: Dict[str, Any] = {"preview": True, "feature": feature.name, "path": rel,
                               "questions": [choices.question(n, contexts[n])
                                             for n in unanswered] + resolve.questions,
                               "choices": shown,
                               "actions": [a.to_dict() for a in plan],
                               "perTest": _per_test(plan), "testSets": _preview_sets(plan),
                               "tagSummary": summarize_tags(
                                   [feature], rules,
                                   existing_set_names(ctx, pk, tag_values([feature]))),
                               "notDone": not_done}
        if analysis.warnings:
            out["warnings"] = list(analysis.warnings)
        if unanswered:
            out["hint"] = ("answer the questions (ask the user once), then call again with the "
                           "answers; until then the actions assume createTestSet=true, "
                           "backgrounds=preconditions, tagsAsLabels=true and no AI label")
        elif resolve.questions:
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
    requiredfields.commit(ctx)
    run = _Run(ctx, pk, plan, skip, test_extras, pre_extra, set_extra)
    result = run.run()
    out = {"feature": feature.name, "path": rel, "created": run.created, "updated": run.updated,
           "tests": [{"index": i, "key": run.test_keys[i]} for i in sorted(run.test_keys)]}
    out.update(result)
    out["testSetsCreated"] = run.sets_created
    out["testSetMemberships"] = run.memberships
    if run.fields_applied:
        out["fieldsApplied"] = run.fields_applied
    for item_type, applied in resolve.applied.items():
        requiredfields.add_applied(out, item_type, applied)
    out["notDone"] = not_done
    if skipped:
        out["skippedDuplicates"] = skipped
        out["hint"] = "pass the skipped indexes in createDuplicates to create them anyway"
    warnings = list(analysis.warnings) + run.warnings
    if warnings:
        out["warnings"] = warnings
    return out
