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
| Result import multipart | Cucumber/Behave sent the results part as `result` | every format's part is `results` (`result` gets HTTP 400 "Unexpected field (result)"); `info` holds the Test Execution fields |
| Cucumber results into an existing execution | refused | Xray Cloud's Cucumber endpoints always create a new execution (`info.xrayFields.testExecKey` -> HTTP 400 "invalid fields: testExecKey"; a `testExecKey` query parameter and `info.key` are ignored). The tool converts the report to Xray JSON with `testExecutionKey` (one status per `@TEST_<KEY>` scenario, outline rows as example statuses, failures as comment); untagged scenarios are refused before upload. Behave into an existing execution is refused |

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
| Feature import: questions / `needs_answer`, answers remembered per project | ok: the first preview asks `createTestSet`, `backgrounds`, `tagsAsLabels`, `aiLabel`, `aiLabelScope`; `apply` without answers is refused with the same questions and writes nothing; the answers are stored on apply under `"<region>/<PROJECT>"` in `preferences.json`; a second preview asks nothing (every source `remembered`) |
| AI label scope `all` on feature import | ok: the label is on the created test, Pre-Condition and Test Set (read back via Xray) |
| AI label on `create_test`, `create_test_set`, `create_test_plan` | partly: live only with `aiLabel: ""` (don't mark; the existing live tests run unchanged); a non-empty label on these single creates is unit-tested only |
| Feature import: `backgrounds=inline` | ok (`regression/inline_backgrounds.feature`): no Pre-Conditions; each test starts with the feature Background, then the Rule Background; data tables (incl. an escaped pipe) and doc strings (media type, blank and indented lines, a ```` ``` ```` block containing `"""`) read back intact |
| Feature import: tag rules | ok: `key=value`, `key:value` and a comma list with `split` give labels; a Test Set created beforehand is reused by exact name and another is created; memberships read back via Xray; a regex `skip` rule drops its tag; an unmatched plain tag is kept as a label; a rule field (`fields.test` mapped to the Jira date field "Start date") is set on create |
| Field read for "never overwrite" | ok: Xray `jira(fields: ["customfield_…"])` returns the current value under the field-id key, so a re-import with `@TEST_` keys lists the set field in `notDone` ("already set to …", value unchanged) and sets the empty field of the other test through Jira (`fieldsApplied`) |
| `notDone` entries | ok for the "already set" case (shape `{item, what, reason, fix}`); the no-token wording is unit-tested only (the live run has a Jira token) |
| `analyze_feature_tags` over `tests/fixtures/gherkin/regression` | ok: all 10 files, no errors, key/value keys `area`, `areas`, `comp`, `suite`, plain tags `team`, `wip` |
| `get_status` with `check=true` | ok: credentials `configured: true` (no values), Jira reachable via `/myself`, preferences file in the temp data dir, all five choices unanswered; no credential value appears in the output |
| `set_preferences` | unit tests only (the live tests remember answers through the import itself) |

## Plan 2: results, native import/export, analysis

Verified live with `tests/live/test_live_plan2.py` (temp settings and `CLAUDE_PLUGIN_DATA`;
everything created, including the tests Xray creates for JUnit test cases, deleted in teardown):

| Item | Outcome |
|---|---|
| `import_execution_results` JUnit, new execution with `summary` | ok: multipart `import/execution/junit/multipart` with parts `info` (Jira fields, summary applied) and `results`; response `{id, key, self}`; runs and `byStatus` match the file |
| `import_execution_results` Cucumber JSON into an existing execution | ok (sent as Xray JSON with `testExecutionKey`): same execution, one run per `@TEST_` key, failure message as run comment, Scenario Outline rows as `examples` statuses (`PASSED`, `FAILED`) |
| `import_execution_results` Cucumber JSON, new execution with `testEnvironments` | ok: multipart with `info.xrayFields.environments`; the execution carries the environment |
| `import_execution_results` Xray JSON (plain `import/execution`) | ok: new execution, `byStatus` from `getTestRuns` |
| `import_feature_file` on `regression/sample.feature` (run-id prefixed copy) | ok: multipart part `file` (with `projectKey` query parameter); response keys `updatedOrCreatedTests`, `updatedOrCreatedPreconditions` (`[{id, key, self}]`) and `errors`; 2 tests (scenario, scenario outline) and 1 Pre-Condition. A re-import of the same file updates the same keys (Xray matches by name). `testInfo` / `precondInfo` parts are as documented; not exercised live (the test project needs no extra fields) |
| `export_cucumber_features` of those keys | ok: one `.feature` file (Xray names it `1_<KEY>,<KEY>.feature`), parses with the vendored parser, `@TEST_` tags and `#@PRECOND_` comment present, Feature name empty; a second export into the same directory is refused (no overwrite) |
| `get_test_runs` | ok: GraphQL `getTestRuns(testIssueIds)` with `status { name }`, `startedOn`, `finishedOn`, `defects`, `testExecution { testEnvironments jira(fields: ["key"]) }` all valid; newest first, `lastStatus`, `flaky` true with PASSED and FAILED, environments per run |
| `get_requirement_coverage` | ok: the Jira link type is `Test` (inward "is tested by", outward "tests"), so `issue in linkedIssues("<KEY>", "is tested by")` finds the covering tests; latest status per test via `getTestRuns(testIssueIds) { test { issueId } ... }` |
| `coverageStatus` | available: `getCoverableIssues(jql) { results { status { name description } } }` (`CoverableIssue.status(environment, isFinal, version, testPlan)`) gave `NOTRUN` before any run and `OK` after a passed run; an issue that is not a requirement type returns no rows (`coverageStatus: null`). Added to `get_requirement_coverage` |
| `find_related_tests` | ok: the created test is found by label, by term and by issue key (`matchedBy` `labels`, `terms`, `issueKeys`); all three at once give one row with all three kinds and the latest status |
| `dev/parity_audit.py` | ok: 66 mutations, 30 queries; every mutation has a row in "API parity" below |

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

## Background steps in Cucumber JSON result imports

After `POST /api/v2/import/execution/cucumber` with a report whose `background` element has
passed steps, the test run's `results[].backgrounds` was **empty**. The Background shows up only
as the run's Pre-Condition (`preconditions.definition = "Given the app is running"`). This
matches the forum report: Background steps are not shown as run steps.

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
needs credentials). Completed from its output on the live tenant (Plan 2, Task 8): every mutation
of the live schema has a row, which `tests/live/test_live_plan2.py` checks. A cell is
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
