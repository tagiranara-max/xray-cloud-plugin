# Live verification against Xray Cloud

The unit tests use fakes whose GraphQL/REST shapes were written from documentation. This file
records what was checked against a real Xray Cloud tenant (region `global`, a throwaway test
project, Jira Cloud with an API token) with `python3 -m pytest -m live` (tests/live/), and what
changed as a result. The live suite drives the tools through the real registry and
`ServerContext`, i.e. the same code path Claude Code uses.

In addition, every GraphQL document in the server was validated statically against the live
schema (introspection): fields, arguments, variable types and required arguments.

## Bugs found and fixed

| Area | Problem (as built from docs) | Verified shape / fix |
|---|---|---|
| `update_test_run` steps | `updateTestRunStep(id, status, comment, actualResult)` does not exist | `updateTestRunStep(testRunId, stepId, updateData: UpdateTestRunStepInput!) { warnings }` |
| `update_test_run` evidence | `[EvidenceInput!]!`, `addedEvidence { id }` | `evidence: [AttachmentDataInput]!`; `addedEvidence` is `[String]` |
| Test environments | `getTestEnvironments` / `createTestEnvironment` / `deleteTestEnvironment` do not exist | list via `getProjectSettings(projectIdOrKey).testEnvironments`; `create_test_environment` / `delete_test_environment` replaced by `add_test_environments_to_test_execution` (unknown names are created) and `remove_test_environments_from_test_execution`. Environments cannot be deleted through the API. |
| Duplicate check, Test Set / Pre-Condition reuse | a `summary ~ "…"` phrase containing `[`/`]` never matches (not even the issue's own summary); a raw TAB makes the JQL invalid | text searches strip Lucene-special characters (`jql_text`); JQL strings escape `\t \n \r` |
| Feature import, non-English outlines | a `Beispiele:` block left the test as `scenarioType: scenario` | the stored definition uses the English `Examples:` keyword (Xray's own import of a `# language: de` file does the same) |
| Run defects | – | `TestRun.defects` returns Jira issue ids, not keys (tool description says so) |
| Permissions | a missing Delete Issues permission surfaced as a raw Xray message | clear `permission_denied` errors (see README "Permissions") |
| Result import response | read `testExecIssue {id, key}` (Xray Server/DC shape): `testExecKey` came back null and the run summary was never read | Xray Cloud answers `{"id", "key", "self"}` on the plain and the multipart endpoints (both shapes are accepted) |
| Result import multipart | Cucumber/Behave sent the results part as `result` | the part is `results`: verified live for JUnit, Cucumber, Behave and Xray JSON (`result` gets HTTP 400 "Unexpected field (result)"); `info` holds the Test Execution fields (Xray JSON multipart honours `info.xrayFields.environments` too) |
| Cucumber results into an existing execution | refused | Xray Cloud's Cucumber endpoints always create a new execution (`info.xrayFields.testExecKey` -> HTTP 400 "invalid fields: testExecKey"; a `testExecKey` query parameter and `info.key` are ignored). The tool converts the report to Xray JSON with `testExecutionKey` (one status per test, outline rows as example statuses, failures as comment). Scenarios are matched to tests before upload (see "Result import matching"); no `@TEST_` tags are needed. Behave goes the same way |
| Tests created silently by result imports | JUnit/TestNG/NUnit/Robot cases with no matching test became new Generic tests without asking | every result is matched before upload; ambiguous or unmatched results return `needs_answer` and nothing is uploaded until the user maps them (`testKeys`), leaves them out (`skipMissing`) or allows creation (`allowCreateTests`, created keys in `createdTests`) |

## Verified behaviour

| Item | Outcome |
|---|---|
| Auth in the configured region | ok |
| `search_tests`, `get_test` (detail), `list_folders`, `get_test_environments` | ok |
| `createTest` with `testType` + `steps` (Manual), `gherkin` (Cucumber), Generic | ok |
| `updateTestType` (Generic → Manual), `updateGherkinTestDefinition`, `deleteTest` | ok |
| `addTestStep` at end with an attachment; insert after step N (tail rebuilt) | ok, attachment carried over |
| `move_test_step` | ok; the attachment downloaded from `downloadLink` (`XrayClient.download`) is byte-identical after re-upload |
| `updateTestStep` with attachment + inline embed; `removeTestStep` | ok |
| Pre-Conditions: create with `testIssueIds`, `updatePrecondition` definition, `addTestsToPrecondition`, matching by path label | ok (re-import reuses the Pre-Condition and updates its definition; Test Set reused by summary; `@TEST_` tags update tests) |
| Test Set / Test Plan: create (with `testKeys`, `newTests`, `testSetKeys`), add/remove, search, delete | ok |
| Test Execution: create with `testPlanKey` + environments, add/remove tests, delete | ok |
| `update_test_run`: step statuses PASSED/FAILED with comment/actualResult, derived run status, evidence upload, `addDefectsToTestRun` | ok |
| Test-run custom fields | **not verifiable**: the test project has no test-run custom fields (`getProjectSettings.testRunCustomFieldSettings.fields == []`) |
| Folders: `createFolder` (idempotent), `addTestsToFolder`, `getTests(folder: {path, includeDescendants})` | ok (`false` = only direct tests, `true` = includes subfolders) |
| `renameFolder`; `moveFolder` | ok; `destinationPath` is the new **parent** folder |
| `deleteFolder` with a subfolder | ok, tests are kept (unfiled); a deleted path then reports "No Valid folder with path …" |
| Jira create-meta (`/issue/createmeta/{project}/issuetypes/{id}`) | ok (`fieldId`, `required`, `schema.type`, `allowedValues`) |
| `edit_issue`: summary, description as ADF, labels via `set_lifecycle_label` | ok |
| `list_project_versions` | ok (project had no versions) |
| Requirement link direction | correct as built: both the `issuelinks` add on create (`outwardIssue` = requirement) and `POST /issueLink` (test = `inwardIssue`, requirement = `outwardIssue`) make the **test** show "tests &lt;story&gt;" |
| Feature import of every `tests/fixtures/gherkin/regression/*.feature` | ok: every test's Gherkin read back re-parses to the same steps, data-table rows (incl. escaped pipes, `\n`, backslashes, empty cells, unicode, tabs) and doc-string lines (blank lines, indentation, media types, escaped delimiters) as the source; Pre-Condition definitions equal the Backgrounds; outlines (incl. `Scenario Template` / `Scenarios:` and German `Szenariogrundriss`) get `scenarioType: scenario_outline`; BOM/CRLF and `# language: de` accepted |

## Import choices, tag rules, preferences and status

Verified live with `tests/live/test_live_import_choices.py` (each test uses its own temp settings
file and temp `CLAUDE_PLUGIN_DATA`, so remembered answers start empty):

| Item | Outcome |
|---|---|
| Feature import: questions / `needs_answer`, answers remembered per project | ok: the first preview asks `createTestSet`, `backgrounds`, `tagsAsLabels`, `aiLabel`, `aiLabelScope`; `apply` without answers is refused with the same questions and writes nothing; the answers are stored on apply under a site-scoped key (verified before site scoping, when the key was `"<region>/<PROJECT>"`; that older key is still read as a fallback) in `preferences.json`; a second preview asks nothing (every source `remembered`) |
| AI label scope `all` on feature import | ok: the label is on the created test, Pre-Condition and Test Set (read back via Xray) |
| AI label on `create_test`, `create_test_set`, `create_test_plan` | partly: live only with `aiLabel: ""` (don't mark; the existing live tests run unchanged); a non-empty label on these single creates is unit-tested only |
| Feature import: `backgrounds=inline` | ok (`regression/inline_backgrounds.feature`): no Pre-Conditions; each test starts with the feature Background, then the Rule Background; data tables (incl. an escaped pipe) and doc strings (media type, blank and indented lines, a ```` ``` ```` block containing `"""`) read back intact |
| Feature import: tag rules | ok: `key=value`, `key:value` and a comma list with `split` give labels; a Test Set created beforehand is reused by exact name and another is created; memberships read back via Xray; a regex `skip` rule drops its tag; an unmatched plain tag is kept as a label; a rule field (`fields.test` mapped to the Jira date field "Start date") is set on create |
| Field read for "never overwrite" | ok: Xray `jira(fields: ["customfield_…"])` returns the current value under the field-id key, so a re-import with `@TEST_` keys lists the set field in `notDone` ("already set to …", value unchanged) and sets the empty field of the other test through Jira (`fieldsApplied`) |
| `notDone` entries | ok for the "already set" case (shape `{item, what, reason, fix}`); the no-token wording is unit-tested only (the live run has a Jira token) |
| `analyze_feature_tags` over `tests/fixtures/gherkin/regression` | ok: all 10 files, no errors, key/value keys `area`, `areas`, `comp`, `suite`, plain tags `team`, `wip` |
| `get_status` with `check=true` | ok: credentials `configured: true` (no values), Jira reachable via `/myself`, preferences file in the temp data dir, all five choices unanswered; no credential value appears in the output |
| `set_preferences` | unit tests only (the live tests remember answers through the import itself) |

## Results, native import/export, analysis

Verified live with `tests/live/test_live_results_analysis.py` (temp settings and `CLAUDE_PLUGIN_DATA`;
everything created, including the tests Xray creates for JUnit test cases, deleted in teardown):

| Item | Outcome |
|---|---|
| `import_execution_results` JUnit, new execution with `summary` | ok: with two unmatched cases the first call answers `needs_answer` (`missing` = the cases' Generic definitions) and creates nothing; with `allowCreateTests` multipart `import/execution/junit/multipart` with parts `info` (Jira fields, summary applied) and `results`; response `{id, key, self}`; runs and `byStatus` match the file; the two new tests are listed in `createdTests` |
| `import_execution_results` Cucumber JSON into an existing execution | ok (sent as Xray JSON with `testExecutionKey`): same execution, untagged Scenario Outline rows matched by the test's summary, one run per test, failure message as run comment, Scenario Outline rows as `examples` statuses (`PASSED`, `FAILED`). The converter also counts a failed Background (Cucumber JSON emits it as its own element before each scenario) and failing scenario or step hooks as FAILED; repeated non-outline scenarios of one key keep the worst status (unit-tested) |
| `import_execution_results` Cucumber JSON, new execution with `testEnvironments` | ok: multipart with `info.xrayFields.environments`; the execution carries the environment; an untagged scenario matched by summary is sent natively with an added `@TEST_<KEY>` tag and lands on that test |
| `import_execution_results` Xray JSON (plain `import/execution`) | ok: new execution, `byStatus` from `getTestRuns` |
| `import_feature_file` on `regression/sample.feature` (run-id prefixed copy) | ok: multipart part `file` (with `projectKey` query parameter); response keys `updatedOrCreatedTests`, `updatedOrCreatedPreconditions` (`[{id, key, self}]`) and `errors`; 2 tests (scenario, scenario outline) and 1 Pre-Condition. A re-import of the same file updates the same keys (Xray matches by name). `testInfo` / `precondInfo` parts are as documented; not exercised live (the test project needs no extra fields) |
| `export_cucumber_features` of those keys | ok: one `.feature` file (Xray names it `1_<KEY>,<KEY>.feature`), parses with the vendored parser, `@TEST_` tags and `#@PRECOND_` comment present, Feature name empty; a second export into the same directory is refused (no overwrite) |
| `get_test_runs` | ok: GraphQL `getTestRuns(testIssueIds)` with `status { name }`, `startedOn`, `finishedOn`, `defects`, `testExecution { testEnvironments jira(fields: ["key"]) }` all valid; newest first, `lastStatus`, `flaky` true with PASSED and FAILED, environments per run |
| `get_requirement_coverage` | ok: the Jira link type is `Test` (inward "is tested by", outward "tests"), so `issue in linkedIssues("<KEY>", "is tested by")` finds the covering tests; latest status per test via `getTestRuns(testIssueIds) { test { issueId } ... }` |
| `coverageStatus` | available: `getCoverableIssues(jql) { results { status { name description } } }` (`CoverableIssue.status(environment, isFinal, version, testPlan)`) gave `NOTRUN` before any run and `OK` after a passed run; an issue that is not a requirement type returns no rows (`coverageStatus: null`). Added to `get_requirement_coverage` |
| `find_related_tests` | ok: the created test is found by label, by term and by issue key (`matchedBy` `labels`, `terms`, `issueKeys`); all three at once give one row with all three kinds and the latest status |
| `dev/parity_audit.py` | ok: 66 mutations, 30 queries on the audited tenant; every one of those mutations has a row in "API parity" below |

## Comparison with Xray's native import

`regression/sample.feature` imported with `create_tests_from_feature` and with
`POST /api/v2/import/feature?projectKey=…`, both exported with
`GET /api/v2/export/cucumber?keys=…`:

- **Equal:** scenarios (steps, data tables, doc strings, outline flag, Examples), and the
  Pre-Condition steps (`Given the app is running`).
- **Differences:**
  - The plugin creates a Test Set for the Feature, so its exported scenarios carry
    `@TESTSET_<KEY>`. The native import creates no Test Set.
  - The plugin labels the Pre-Condition with the feature's path (used for reuse on re-import).
    The native Pre-Condition has no labels.
  - The native import drops the Examples name (it stores a bare `Examples:`). The plugin keeps
    `Examples: <name>`, tags and descriptions.
  - Tests carry the Feature/Scenario tags as labels in both. The sample file has none.

## Result import matching

Probed live on 2026-11-24 (throwaway issues, all deleted afterwards) to find how Xray Cloud maps
imported results to tests, so the tool's pre-check mirrors it:

| Format | What Xray Cloud does |
|---|---|
| Cucumber JSON (multipart, new execution) | Only a `@TEST_<KEY>` tag maps a scenario. An untagged scenario whose name equals the summary of an existing Cucumber test is **not** matched, and **no test is created** for an untagged scenario: it is dropped silently when other scenarios are tagged, and a report with no tagged scenario fails with HTTP 400 "There are no valid tests imported" (no execution created) |
| Behave JSON (multipart, new execution) | Same as Cucumber: tag `TEST_<KEY>` (Behave writes tags without `@`) maps; untagged scenarios are dropped silently |
| JUnit | A `<testcase classname="a.B" name="c">` with no match creates a **Generic** test with summary `c` (the case name) and definition (`unstructured`) `a.B.c`. A second import of the same case reuses that test (matched by definition). An existing Generic test whose definition is `classname.name` is reused whatever its summary (the summary is kept). An existing Generic test with the same summary but another definition is **not** matched: a new test is created. `<property name="test_key" value="KEY"/>` maps the case to that test, of any type (Manual included), summary kept |
| TestNG | Definition `<class name>.<test-method name>`, summary = method name; `<attributes><attribute name="test">KEY</attribute></attributes>` maps to that test |
| NUnit 3 | Definition = `test-case@fullname`, summary = `name`; `<property name="Test" value="KEY"/>` maps to that test |
| Robot | Definition = `<suite name>.<test name>`, summary = test name; a tag equal to a test key maps to that test **and renames that test** to the Robot test's name (the plugin prevents this, see below) |
| Xray JSON `testInfo` (no `testKey`) | `type: Cucumber` takes the steps in `scenario` (`definition` gets HTTP 400 "Test of kind gherkin should not contain ... definition or steps") and always creates a new test, even when an identical one exists; `type: Generic` with `definition` reuses the Generic test with that definition **and overwrites its summary** with `testInfo.summary` |

Second probe, 2026-11-24 (does an import change the existing test?):

| Case | Result |
|---|---|
| Cucumber JSON (native, new execution) with `@TEST_<KEY>` on a Cucumber test and on a Manual test, other scenario names and steps | nothing changed: type, gherkin, summary and labels kept; runs created on both tests |
| Xray JSON entry with `testKey` and `testInfo` (Generic, other summary and definition) | the test's **summary and definition were overwritten** from `testInfo` |
| JUnit case with `test_key` (Manual test) and properties `test_summary`, `test_description`, `tags` | the test's **summary was overwritten and the tags added as labels** |
| JUnit case matched by Generic definition with the same properties | same: **summary overwritten, labels added** |
| GraphQL `getTests(jql: "issue in (...)")` with a key that does not exist (`ISO-8601`, an unused number) | no error: the key is just not in the results |
| Robot test matched by Generic definition only (no key tag), with tags `smoke`, `rtagdef` and a `<doc>` | the test was **renamed** to the Robot test's name and the **tags were added as labels**; the description was kept (the plugin prevents both, see below) |
| Robot test matched by a key tag (Manual test), with tags `smoke`, `rtagkey` and a `<doc>` | **renamed**, **tags added as labels**; description kept (prevented by the plugin, see below) |
| Robot test named exactly like the Generic test's summary, only its key tag, definition `Suite.<name>` equal to the test's | **nothing changed** (summary, definition, labels, type) |
| Same, but the test's definition differs from the Robot `Suite.Test` path | **nothing changed**: the key tag wins and the definition is not overwritten |

So the tool sends known tests by `testKey` only (no `testInfo`); removes JUnit `test_summary` /
`test_description` / `tags` (and the similar TestNG/NUnit names, unverified, as a precaution)
and Robot plain (not key-shaped) tags from cases matched to existing tests; and refuses
`testKeys` values that are not existing Tests of the project. For every Robot result matched
to an existing test (by key tag, `testKeys` or definition), the uploaded copy gets the test's
key tag and the test's current summary as the Robot test name, so Xray renames nothing. The
user's file is never modified. TestNG and NUnit behaviour on matched tests was not verified
live: their property stripping is a precaution. Requirement links named in the file
(JUnit `requirements`, Robot requirement-key tags and the like) are kept, so an import can add
requirement links to existing tests; plain Cucumber/Behave tags also stay in the uploaded copy.

So, in the tool: Cucumber/Behave scenarios are matched by a `@TEST_<KEY>` tag or the project's
`testKeyTag` tag (if they name different tests, the tool asks), else by exact summary (the plugin's own rule; trimmed, case-sensitive), and the resolved key is sent as
a `@TEST_<KEY>` tag (new execution, native import keeps step details) or as `testKey` in Xray
JSON (existing execution, or tests to create: those go as `testInfo` with `type: Cucumber`).
XML cases mirror Xray's rule (file key, else Generic definition); Robot key tags count only when
the issue is a Test. The Generic-definition lookup pages `getTests(jql, testType: {name:
"Generic"})` (at most 5000 tests; past that, unmatched cases are reported with a note, and Xray
may still reuse a test it finds).

## Background steps in Cucumber JSON result imports

After `POST /api/v2/import/execution/cucumber` with a report whose `background` element has
passed steps, the test run's `results[].backgrounds` was **empty**. The Background shows up only
as the run's Pre-Condition (`preconditions.definition = "Given the app is running"`). This
matches the forum report: Background steps are not shown as run steps.

## Required Jira fields without the Jira token

Probed live on 2026-11-24 with Xray credentials only (no Jira token in the probing process).
Components was made required, temporarily, for the Test issue type of the live test project
(the project the live suite writes to); that project had no components.

| `createTest` with | Result |
|---|---|
| no `components` | GraphQL error `Create issue failed! - components: Components is required., Components: Components is required.`; **nothing created** (JQL by the unique summary: 0 issues) |
| `"components": []` (by field id, empty) | `Create issue failed! - components: Components is required.`; nothing created |
| `"Components": [{"name": ...}]` (by field name) | `Create issue failed! - Components: Field 'Components' cannot be set. It is not on the appropriate screen, or unknown.`; nothing created |
| `"components": [{"name": "<new name>"}]` (by field id, a component that did not exist) | **created**, and Jira **created the component** on the fly (the probing user administers the project); the test was deleted at once and the component left for the owner to remove |

So Jira's "X is required" error reaches the caller through Xray, names each field by id (and
again by name), and nothing is created; `jira.fields` takes field ids only, not names. The
plugin therefore parses the field ids and names from the error (`<id>: <Name> is required.`),
learns them for the server run, drops the cached create-meta of that project and item type,
and runs the call once more, which asks for them like discovered fields (elicitation for
simple fields, otherwise `needs_answer`); the answers are converted for known system fields
(`components`, `fixVersions`, `priority`, ...) and remembered per field. A component name
that does not exist yet may be created by Jira when the user can administer the project.

- When such a refusal comes after the call already wrote other items (a container's later new
  test, a feature import's Pre-Condition or Test Set), the call stops with `partial_failure`
  naming the item not created and the parameter for its values (`testFields`,
  `preconditionFields`, `testSetFields`). A container whose first new test is refused is
  deleted again and the call asks, as above.
- A "Create issue failed!" error naming no readable field returns `needs_answer` with Jira's
  raw message and the field-ids guidance; nothing is retried automatically. An error naming
  only system fields (for example the reporter) is passed through unchanged.
- Answers for an item type are remembered only after a create of that item type succeeded
  (a container's answers on the container create, `testFields` answers on its first new
  test), so a container rolled back after its first test was refused leaves the test answers
  unsaved. If that rollback itself fails, the `partial_failure` still lists the container in
  `created`, with the delete error in `rollbackError`.
- Known gap: `import_execution_results` and `import_feature_file` go through Xray's REST
  import, which is not wrapped; Jira's required-field error is passed through without asking.
- No live test covers this path: it needs a field made required in Jira, a temporary change
  by the project owner. It is covered by unit tests built from the recorded error text.

## Notes

- Xray answered with HTTP 502 or a connection reset a few times during the runs. When it
  happened on a mutation, the mutation had sometimes taken effect anyway (the plugin's
  partial-failure reports handled this correctly). Both clients therefore retry only
  idempotent requests: GraphQL queries and HTTP GETs are repeated up to twice (after 0.5 s and
  1.5 s, through the rate limiter) on 502/503/504, connection resets and timeouts. Mutations,
  POST, PUT and DELETE are never retried.
- The test environment `xray-cloud-live` created by the live suite stays in the project: Xray
  has no API to delete environments.

## API parity

Every GraphQL mutation the server uses and every REST import/export endpoint, with the tool
that exposes it. `dev/parity_audit.py` lists the mutations and queries of the live schema (read-only,
needs credentials). Completed from its output for the audited tenant (66
mutations): every mutation of that tenant's schema has a row, which
`tests/live/test_live_results_analysis.py` checks. Other tenants or later Xray releases may add mutations;
rerun the audit. A cell is
`tool: <name>[, <name>...]` or `not exposed: <reason>`; `tests/unit/test_parity_doc.py` checks
that every named tool is registered.

| API | Exposed as |
|---|---|
| `createTest` | tool: create_test, create_tests_from_feature |
| `updateTestType` | tool: update_test_type |
| `updateGherkinTestDefinition` | tool: edit_test |
| `deleteTest` | tool: delete_test |
| `addTestStep` | tool: add_test_steps, move_test_step |
| `updateTestStep` | tool: edit_test_step, move_test_step, add_test_steps |
| `removeTestStep` | tool: remove_test_step, move_test_step, add_test_steps |
| `createTestSet` | tool: create_test_set, create_tests_from_feature |
| `addTestsToTestSet` | tool: add_tests_to_test_set |
| `removeTestsFromTestSet` | tool: remove_tests_from_test_set |
| `deleteTestSet` | tool: delete_test_set |
| `createTestPlan` | tool: create_test_plan |
| `addTestsToTestPlan` | tool: add_tests_to_test_plan |
| `removeTestsFromTestPlan` | tool: remove_tests_from_test_plan |
| `deleteTestPlan` | tool: delete_test_plan |
| `addTestExecutionsToTestPlan` | tool: add_test_executions_to_test_plan |
| `createTestExecution` | tool: create_test_execution |
| `addTestsToTestExecution` | tool: add_tests_to_test_execution |
| `removeTestsFromTestExecution` | tool: remove_tests_from_test_execution |
| `deleteTestExecution` | tool: delete_test_execution |
| `addTestEnvironmentsToTestExecution` | tool: add_test_environments_to_test_execution |
| `removeTestEnvironmentsFromTestExecution` | tool: remove_test_environments_from_test_execution |
| `updateTestRun` | tool: update_test_run |
| `updateTestRunStatus` | tool: update_test_run |
| `updateTestRunStep` | tool: update_test_run |
| `addDefectsToTestRun` | tool: update_test_run |
| `addEvidenceToTestRun` | tool: update_test_run |
| `createPrecondition` | tool: create_tests_from_feature |
| `updatePrecondition` | tool: create_tests_from_feature |
| `addTestsToPrecondition` | tool: create_tests_from_feature |
| `createFolder` | tool: create_folder |
| `renameFolder` | tool: rename_folder |
| `moveFolder` | tool: move_folder |
| `deleteFolder` | tool: delete_folder |
| `addTestsToFolder` | tool: add_tests_to_folder |
| `removeAllTestSteps` | not exposed: remove_test_step removes one step; the tail rebuild removes steps one by one |
| `updateUnstructuredTestDefinition` | not exposed: Generic test definitions are set on create only |
| `updateTestFolder` | not exposed: add_tests_to_folder (`addTestsToFolder`) moves tests between folders |
| `updatePreconditionFolder` | not exposed: Pre-Conditions are not filed in folders by the plugin |
| `addIssuesToFolder` | not exposed: the plugin files tests only (add_tests_to_folder) |
| `removeIssuesFromFolder` | not exposed: the plugin files tests only |
| `removeTestsFromFolder` | not exposed: tests are moved with add_tests_to_folder; delete_folder keeps them unfiled |
| `deletePrecondition` | not exposed: no Pre-Condition delete tool yet (the live suite cleans up with it directly) |
| `addPreconditionsToTest` | not exposed: create_tests_from_feature links Pre-Conditions from the Pre-Condition side (`addTestsToPrecondition`) |
| `removePreconditionsFromTest` | not exposed: no unlink tool for Pre-Conditions yet |
| `removeTestsFromPrecondition` | not exposed: no unlink tool for Pre-Conditions yet |
| `addTestSetsToTest` | not exposed: covered from the Test Set side (add_tests_to_test_set) |
| `removeTestSetsFromTest` | not exposed: covered from the Test Set side (remove_tests_from_test_set) |
| `addTestPlansToTest` | not exposed: covered from the Test Plan side (add_tests_to_test_plan) |
| `removeTestPlansFromTest` | not exposed: covered from the Test Plan side (remove_tests_from_test_plan) |
| `addTestExecutionsToTest` | not exposed: covered from the execution side (add_tests_to_test_execution) |
| `removeTestExecutionsFromTest` | not exposed: covered from the execution side (remove_tests_from_test_execution) |
| `removeTestExecutionsFromTestPlan` | not exposed: no unlink tool for plan executions yet (only add_test_executions_to_test_plan) |
| `updateTestRunComment` | not exposed: update_test_run sets the comment through `updateTestRun` |
| `updateTestRunStepStatus` | not exposed: update_test_run sets step status through `updateTestRunStep` |
| `updateTestRunStepComment` | not exposed: update_test_run sets step comments through `updateTestRunStep` |
| `updateTestRunExampleStatus` | not exposed: example (outline row) statuses come from result imports (import_execution_results) |
| `updateIterationStatus` | not exposed: data-driven iterations are not handled by the plugin |
| `addDefectsToTestRunStep` | not exposed: defects are added at run level only (update_test_run) |
| `addEvidenceToTestRunStep` | not exposed: evidence is added at run level only (update_test_run) |
| `removeDefectsFromTestRun` | not exposed: update_test_run only adds defects |
| `removeDefectsFromTestRunStep` | not exposed: step-level defects are not handled |
| `removeEvidenceFromTestRun` | not exposed: update_test_run only adds evidence |
| `removeEvidenceFromTestRunStep` | not exposed: step-level evidence is not handled |
| `resetTestRun` | not exposed: destructive (clears a run's results); not offered to the agent |
| `setTestRunTimer` | not exposed: timers are a UI feature; imports and update_test_run set start/finish |
| REST `import/execution*` (JUnit, TestNG, NUnit, Robot, Cucumber, Behave, Xray JSON, with `/multipart`) | tool: import_execution_results |
| REST `import/feature` | tool: import_feature_file |
| REST `export/cucumber` | tool: export_cucumber_features |

## Client support for MCP elicitation

The server asks for write approval and missing required fields by MCP elicitation when the client declares the capability at initialize. Otherwise it returns `needs_answer` and the agent asks. Checked 2026-11-24 against each client's official documentation:

| Client | Elicitation | Source |
|---|---|---|
| Claude Code | yes (interactive dialog in terminal sessions; not in `claude -p` runs) | https://code.claude.com/docs/en/mcp (section "Respond to MCP elicitation requests") |
| Cursor | yes | https://cursor.com/docs/context/mcp (protocol support table: Elicitation) |
| VS Code (Copilot agent) | yes (form and URL, during an active turn) | https://code.visualstudio.com/docs/copilot/guides/mcp-developer-guide |
| OpenAI Codex | unknown: the official MCP page does not mention elicitation | https://developers.openai.com/codex/mcp |
| Gemini CLI | unknown: the official MCP page does not mention elicitation | https://geminicli.com/docs/tools/mcp-server/ |

Where a client's documentation is silent the README says "the agent asks you"; behaviour can differ by version, and some clients have open bug reports about prompts not showing (for example in specific windows or platforms). The server falls back to `needs_answer` when the client does not declare the capability.

## Native plugin manifests (Codex, Cursor)

Checked 2026-11-25 against the official documentation and, where the documentation is silent, the Codex source (github.com/openai/codex, `main`). Nothing was installed; the manifests are covered by `tests/unit/test_native_manifests.py`, not by a live install.

| File | Field | Confirmed against |
|---|---|---|
| `.agents/plugins/marketplace.json` | location; `name`, `interface.displayName`, `plugins[].name`, `source: {source: "local", path: "./plugin"}` (path relative to the marketplace root, `./` prefix, inside the root), `policy.installation: "AVAILABLE"`, `policy.authentication: "ON_INSTALL"`, `category` (all three required) | https://developers.openai.com/codex/plugins/build (sections "Marketplace metadata", "How local marketplaces work") |
| same | `codex plugin marketplace add owner/repo`, then `/plugins` in the CLI | https://developers.openai.com/codex/plugins/build ("Add a marketplace from the CLI"), https://developers.openai.com/codex/plugins |
| same | precedence over the repo's `.claude-plugin/marketplace.json` | Codex source `core-plugins/src/marketplace.rs` (`.agents/plugins/marketplace.json` is read first) |
| `plugin/.codex-plugin/plugin.json` | compatibility manifest location; `name`, `description`, `author`, `license`, `keywords`, `skills: "./skills/"`, `mcpServers` as a `./` path to a companion file, `interface.displayName/shortDescription/developerName/category`; no `version` needed | https://developers.openai.com/plugins/build/plugins ("Plugin creator output", "Path rules", "Add OpenAI-specific metadata"); `version` optional per Codex source `core-plugins/src/manifest.rs` (`version: Option<String>`) |
| same | discovered before `.claude-plugin/plugin.json` in the same folder | Codex source `exec-server-protocol/src/protocol.rs` (`DISCOVERABLE_PLUGIN_MANIFEST_PATHS`) |
| `plugin/codex-mcp.json` | stdio keys `command`, `args`, `cwd`, `env_vars` (forward named variables from Codex's environment) | https://developers.openai.com/codex/mcp ("STDIO servers") |
| same | the plugin MCP file is read into the same server config as `config.toml` (so `env_vars` applies), and a relative `cwd` is resolved against the plugin root | Codex source `codex-mcp/src/plugin_config.rs` (not in the docs) |
| `plugin/.cursor-plugin/plugin.json` | `name` (required), `description`, `author.name`, `license`, `keywords`, `variables` (JSON Schema with `type`, `title`, `description`, `default`, `enum`, `required`), auto-discovery of `skills/` and `mcp.json` | https://cursor.com/docs/reference/plugins ("Cursor Plugin manifest", "Variables", "Cursor Plugin component discovery") |
| `plugin/mcp.json` | `${CURSOR_PLUGIN_ROOT}` in `args`; `${VAR}` placeholders for declared variables in `env` | https://cursor.com/docs/reference/plugins ("MCP servers") |
| `.cursor-plugin/marketplace.json` | repo-root location; `name`, `owner.name`, `metadata.description`, `plugins[].name/source/description`; `source` is the plugin folder (`plugin`) | https://cursor.com/docs/reference/plugins ("Cursor multi-plugin repositories") |
| README Cursor install | team marketplace import (Teams/Enterprise), local folder `~/.cursor/plugins/local/<name>` (symlinks to folders outside it are skipped) | https://cursor.com/docs/plugins ("Team marketplaces", "Test plugins locally") |

Not confirmed:

- **Codex secrets.** Codex has no install-time prompt for a local (stdio) server's settings; the documented `auth` options apply to HTTP servers. The plugin therefore forwards the variables with `env_vars`, and the user exports them. The ChatGPT desktop app does not read the shell profile; the README gives `launchctl setenv` (macOS, not tried here); other platforms are not covered.
- **Codex working directory.** The server runs in the plugin folder (`cwd: "."`), because the compatibility format expands no plugin-root variable in `args`. Codex passes no project directory, so file paths must be absolute unless `XRAY_PROJECT_DIR` is set per launch (`XRAY_PROJECT_DIR="$PWD" codex`). Settings do not depend on it: the settings file is `XRAY_SETTINGS_FILE` or `~/.config/xray-cloud/settings.yml`, never the project folder. The portable Agent Plugins format (root `plugin.json` + `mcp.json` with `${PLUGIN_ROOT}`) was not used: Cursor reads the same root `mcp.json` but does not expand `${PLUGIN_ROOT}`.
- **Cursor variables for local plugins.** The docs say variable values are set under Plugins, Configure (by team admins for team marketplaces). Whether Cursor offers Configure, or substitutes the variables, for a plugin in `~/.cursor/plugins/local` is not documented. An unset variable left as a literal `${...}` is ignored by the server.
- **Cursor project directory.** The docs do not say which working directory a plugin's stdio server gets, or whether `${workspaceFolder}` (documented for `.cursor/mcp.json`) is expanded in a plugin's `mcp.json`. The plugin sets `XRAY_PROJECT_DIR` to `${workspaceFolder}`; an unexpanded placeholder is ignored by the server.
- **Cursor secrets.** Cursor's `variables` schema has no secret or sensitive flag (the accepted keywords are `type`, `title`, `description`, `default`, `enum`, `const`, `properties`, `required`, `items` and length/numeric constraints), so the client ID, secret and Jira token are not marked as secrets in the manifest; Cursor says the plugin never stores the values.
- **Unknown project folder.** When neither `XRAY_PROJECT_DIR` nor `CLAUDE_PROJECT_DIR` is set and the server's working directory is inside its own plugin folder (Codex), the server treats the project folder as unknown: relative file paths are refused with a "pass an absolute path" error, and `get_status` reports `projectDir`.
- **Personal Cursor install from GitHub.** No documented "add marketplace from a repo" exists for individual users; only team marketplaces (admin import) and the local folder.
