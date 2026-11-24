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
