# Tools and workflows

The tools the server exposes, and how result import, analysis and feature import behave. Setup is in [configuration.md](configuration.md).

## Tools

| Tool | What it does |
|---|---|
| `search_tests` | Search tests in a project by summary text or issue key (query) and/or a JQL filter (jql) |
| `get_test` | Get one test by key |
| `create_test` | Create a test (Manual, Cucumber or Generic) in a project |
| `update_test_type` | Change a test's type (Manual, Cucumber or Generic) |
| `edit_test` | Edit a test (Gherkin through Xray; Jira fields need the Jira token) |
| `delete_test` | Delete a test |
| `set_lifecycle_label` | Set a lifecycle label on a test, replacing any previous lifecycle label |
| `get_test_steps` | Get the steps of a test |
| `add_test_steps` | Add steps to a Manual test at the end, start, or after step N |
| `move_test_step` | Move a step of a Manual test from one position to another |
| `edit_test_step` | Update a step of a Manual test |
| `remove_test_step` | Delete a step of a Manual test |
| `create_test_set` | Create a test set, optionally adding existing or new tests |
| `search_test_sets` | Search test sets in a project |
| `get_tests_in_test_set` | List the tests in a test set |
| `add_tests_to_test_set` | Add tests to a test set |
| `remove_tests_from_test_set` | Remove tests from a test set |
| `delete_test_set` | Delete a test set (the tests are kept) |
| `create_test_plan` | Create a test plan, optionally adding existing or new tests |
| `search_test_plans` | Search test plans in a project |
| `get_tests_in_test_plan` | List the tests in a test plan |
| `add_tests_to_test_plan` | Add tests (or all tests of test sets) to a test plan |
| `remove_tests_from_test_plan` | Remove tests from a test plan |
| `delete_test_plan` | Delete a test plan (the tests are kept) |
| `create_test_execution` | Create a test execution |
| `add_test_executions_to_test_plan` | Link existing test executions to a test plan |
| `add_tests_to_test_execution` | Add tests, tests of test sets, or tests of a plan to an execution |
| `remove_tests_from_test_execution` | Remove tests from a test execution |
| `delete_test_execution` | Delete a test execution and its runs (irreversible) |
| `search_test_executions` | Search test executions in a project |
| `get_test_execution` | Get a test execution and its test runs |
| `update_test_run` | Update a run: status, steps, defects, evidence, run custom fields |
| `get_test_environments` | List the test environments known in a project |
| `add_test_environments_to_test_execution` | Add environments to a test execution (new names are created) |
| `remove_test_environments_from_test_execution` | Remove environments from a test execution (Xray has no API to delete an environment) |
| `create_folder` | Create a Test Repository folder (and missing parents) |
| `add_tests_to_folder` | Move tests into a folder |
| `get_tests_in_folder` | List the tests directly inside a folder |
| `list_folders` | List the folder tree below a path |
| `rename_folder` | Rename a folder |
| `move_folder` | Move a folder with its contents under another folder |
| `delete_folder` | Delete a folder and its subfolders (tests are kept) |
| `create_tests_from_feature` | Import a Gherkin `.feature` file (preview, then apply) |
| `import_execution_results` | Import test results (xray, cucumber, behave, junit, testng, nunit, robot) into a new or existing test execution |
| `import_feature_file` | Import a `.feature` file or a `.zip` of them with Xray's native importer (no duplicate check, choices, tag rules or AI label) |
| `export_cucumber_features` | Export Cucumber tests as `.feature` files into a local directory (never overwrites unless asked) |
| `get_test_runs` | Run history of one test: statuses, counts, last status and flaky flag |
| `get_requirement_coverage` | Tests covering a requirement, each with the status of its latest run |
| `find_related_tests` | Find tests related to an area by terms, requirement keys, labels, components or folder |
| `get_create_fields` | List an issue type's fields: from Jira with the token, otherwise from your settings file |
| `list_project_versions` | List a Jira project's versions (needs the Jira token) |
| `analyze_feature_tags` | Summarise the tags in `.feature` files and show which tag rules match (read-only) |
| `set_preferences` | Change the remembered import choices and tag rules of a project (writes only the plugin's own file) |
| `get_status` | Show the plugin's state: region, credentials configured (never their values), settings file, projects, Jira, and the effective import choices with their source |

## Skills

Skills guide Claude through multi-step workflows on top of the tools:

- **`xray-setup`**: check your setup and, optionally, build a team settings file (field IDs, defaults, import choices, tag rules) from your answers.
- **`import-results`**: import a test result file into Xray as a test execution and check for flaky tests.
- **`import-tests`**: import tests from `.feature` files and from tabular or markdown sources.
- **`predict-affected-tests`**: predict which tests a code change affects and optionally create an execution for the at-risk ones.

## Results and native feature import/export

- **`import_execution_results`** uploads a result file (JUnit, TestNG, NUnit, Robot, Cucumber JSON, Behave JSON or Xray JSON) into a new or existing test execution (max 25 MB; give either `filePath` or `content`). It never lets tests be created without your consent (see Result matching below). Every key (`testExecKey`, `testPlanKey`, `testKeys`, and every key in the report) must belong to `projectKey`; an Xray JSON report gets `info.project` set when missing. Cucumber and Behave results for an existing execution, or with tests to create, are sent as Xray JSON (one status per test, failures as the run comment), because Xray's Cucumber and Behave imports always create a new execution and never create tests. New Cucumber/Behave executions go through the multipart endpoint so the project is set.
- **`import_feature_file`** uses Xray's native importer for a `.feature` file or a `.zip` of them. It is faster and a good fit for bulk loads, but it does no duplicate check, asks no questions, applies no tag rules, adds no AI label and has no preview.
- **`create_tests_from_feature`** is the plugin's own import (see Feature import below): duplicate check, import choices, tag rules, the AI label and a preview before anything is written. Use it for everyday imports; use `import_feature_file` when you want Xray's native result as is.
- **`export_cucumber_features`** writes Cucumber tests (by key or saved filter) as `.feature` files into a local directory and refuses to overwrite existing files unless you ask.

### Result matching

Before any upload, `import_execution_results` works out which test of the project each result goes to:

| Format | Matched by |
|---|---|
| Cucumber, Behave | a `@TEST_<KEY>` tag or the project's `testKeyTag` tag (if they name different tests, the tool asks), else the test whose summary is exactly the scenario name (trimmed, case-sensitive; outline rows use the outline's name). No tags needed |
| JUnit, TestNG, NUnit, Robot | Xray's own rule: the key the file names (JUnit property `test_key`, TestNG attribute `test`, NUnit property `Test`, Robot tag equal to a Test key), else the Generic test with the same definition (JUnit `classname.name`, TestNG `class.method`, NUnit `fullname`, Robot `Suite.Test`). Xray does not match these by summary |
| Xray JSON | `testKey`; an entry with only `testInfo` matches the Generic test with the same definition (Generic type only) |

When a result matches several tests (`ambiguous`) or none (`missing`), nothing is uploaded: the tool answers `needs_answer` with both lists (50 each at most, with counts). Call it again with:

- `testKeys: {name: KEY}` to send results to chosen tests (for XML formats the key is written into the file the way Xray reads it);
- `skipMissing: true` to leave unmatched results out;
- `allowCreateTests: true`, only with the user's consent, to let new tests be created: Xray creates a Generic test per JUnit/TestNG/NUnit/Robot case (summary = the case name), and a Cucumber test named after each unmatched scenario. They are listed in `createdTests` and get no AI label (label them afterwards with `edit_test` or `set_lifecycle_label`, or create labelled tests first with `create_tests_from_feature`).

Existing tests are never changed (except requirement links named in the file: JUnit `requirements`, Robot requirement-key tags and the like are kept, so Xray may link the test to those requirements). Xray would change them in these cases (verified live), so the tool prevents it:

- A Robot result renames the test it maps to (by key tag or by definition) to the Robot test's name, and adds its plain tags as labels. For each Robot test matched to an existing test, the uploaded copy (never your file) carries that test's key tag and its current summary as the name, and drops plain tags (key-shaped tags stay), so the test stays as it is.
- `testInfo` overwrites a test's summary and definition, even next to a `testKey`. Entries whose test is known are sent with `testKey` only.
- JUnit `test_summary` and `tags` properties overwrite the summary and labels of a test matched by key or definition. The tool removes them (and `test_description`; for TestNG and NUnit the similar names, as a precaution) from matched cases and says so in `warnings`. Cases for new tests keep them. TestNG and NUnit behaviour on matched tests was not verified live; their stripping is a precaution.
- Plain Cucumber/Behave tags (for example `@smoke`) stay in the uploaded copy; only the `@TEST_<KEY>` tag is added or replaced.

`testKeys` values and the keys in `@TEST_<KEY>` / `testKeyTag` tags must be existing Tests of the project. A Robot tag counts as a test key only when the issue is a Test (requirement links and tags such as `ISO-8601` are left alone); a Test of another project is refused. Names that occur more than once are told apart: `Feature: Scenario` for Cucumber/Behave, `summary (definition)` for Xray JSON. Tests created from Cucumber hold only the scenario's steps (no Background, no Examples); outline rows with filled-in names show up as missing, so map them with `testKeys`.

## Analysis

Read-only tools for questions about test history and impact:

- **`get_test_runs`**: the run history of one test, with counts by status, the last status and a flaky flag (both PASSED and FAILED among the runs).
- **`get_requirement_coverage`**: the tests that cover a requirement and the latest status of each, with counts and never-run tests.
- **`find_related_tests`**: tests related to an area by text, requirement keys, labels, components or folder; each test appears once with the criteria it matched (at most 200 results).

## Feature import

`create_tests_from_feature` follows Xray's own import semantics:

| Gherkin | Xray |
|---|---|
| Feature | Test Set (reused by summary, if you choose so; see Choices and preferences); feature tags go through the tag rules, plain tags become labels |
| Background (feature or rule level) | Pre-Condition (or inline steps, your choice) linked to every test it applies to; reused by `#@PRECOND_<KEY>` comment, then feature path label, then summary |
| Rule | Its scenarios import normally; its Background combines with the feature's |
| Scenario / Example | Cucumber test (Scenario) whose definition is its steps |
| Scenario Outline / Template | Cucumber test (Scenario Outline) with its steps and all Examples blocks |
| Plain tags | Labels |
| `@TEST_<KEY>` or `@<KEY>` on a scenario | Updates that existing test |
| `@REQ_<KEY>` (or a plain key on the Feature/Rule) | Links new tests to that requirement |
| DocStrings and DataTables | Preserved exactly, with correct escaping |

By default (`apply=false`) the tool only previews what would be created, updated and linked, with possible duplicates flagged. Call it again with `apply=true` to execute; scenarios with possible duplicates are skipped unless their index is listed in `createDuplicates`. A file that fails to parse creates nothing and reports the line and column.

### Choices and preferences

A few import choices are yours to make, and the plugin asks about them instead of guessing:

- `createTestSet`: turn the Feature into a Test Set?
- `backgrounds`: Backgrounds as Pre-Conditions (`preconditions`) or copied into each test (`inline`)?
- `tagsAsLabels`: turn plain tags (such as `@smoke`) into labels?
- `tagRule.<key>`: what a `key:value` or `key=value` tag means when no tag rule covers its key (see "Tag rules").
- `aiLabel` and `aiLabelScope`: mark what the AI creates (see "AI label" below).

Each is asked once per project. When a tool answers `status: needs_answer` (or an import preview returns `questions`), Claude asks you and calls again with your answer, and the plugin remembers it for that project. Nothing is written to Xray until every applicable question is answered.

Where a value comes from, first match wins: the call itself, then your settings file (the project's `featureImport` / `aiCreated`, then the top-level ones), then the remembered answer, then unanswered. A value in the settings file always wins, so a team can fix choices for everyone. A call value that differs from the remembered one applies to that call only, unless `remember: true` is passed.

To change a remembered choice, tell Claude "change my import preferences" (it calls `set_preferences`), or edit the settings file. Remembered answers live in `preferences.json` inside `XRAY_DATA_DIR` (which wins), else `${CLAUDE_PLUGIN_DATA}`, else `~/.config/xray-cloud`; they are per user and survive plugin updates.

```yaml
# Optional, in your settings file; every key is optional (absent = asked or remembered)
featureImport:
  createTestSet: true
  backgrounds: preconditions   # or inline
  tagsAsLabels: true
  unmatchedTags: keep          # keep (default) | skip
aiCreated:
  label: my-ai-label           # your own label; null = don't mark
  scope: tests                 # tests | all
projects:
  PROJ:
    featureImport: {}          # overrides the top level key by key; tagRules replace, not merge
```

### Backgrounds

With `preconditions` (the Xray way), each Background becomes a Pre-Condition linked to the tests it applies to, and is reused on re-import. With `inline`, no Pre-Conditions are created: each test's steps start with the applicable Background steps (feature level, then rule level). `#@PRECOND_<KEY>` comments are ignored with a warning when inline.

### AI label

You can mark what the AI creates with a label of your own choosing. There is no default label, and it has nothing to do with lifecycle labels. Pass `aiLabel` (or set `aiCreated.label`) to a label without whitespace, or `""` to not mark. It is added only to newly created items, never to existing ones. On `create_test_set` and `create_test_plan` it applies to new tests only. For feature import, `aiLabelScope` is `tests` or `all` (`all` also marks new Pre-Conditions and Test Sets).

### Tag rules

Tag rules turn Gherkin tags into labels, Test Set membership and field values. **There are no built-in conventions**: with no rules, plain tags simply become labels (`unmatchedTags: keep`, the default), and a `key:value` or `key=value` tag is never turned into anything silently: `create_tests_from_feature` asks once per key (question `tagRule.<key>`, both separators count as the same key) whether it is a label as written (`suite:regression`), a label with only the value (`regression`), a Test Set (name template, default `Suite {value}`), a Jira field (a `fields.test` name or a `customfield_<n>` id) or ignored. Answer with `tagRuleAnswers: {"suite": {"action": "label_as_is" | "label_value" | "test_set" | "field" | "skip", "testSet": "...", "field": "..."}}`; the answer is remembered as a tag rule of the project, so later values of the key are not asked about. This applies to new and existing tests, and nothing is written until every such question is answered. With `unmatchedTags: skip` uncovered tags are dropped and nothing is asked. Issue-key tags (`@TEST_<KEY>`, `@REQ_<KEY>`, plain keys) are never subject to rules. The rules below are **examples only**, with made-up tag names; write your own, or ask Claude to run `analyze_feature_tags` on your files and propose rules (it explains each one and applies nothing until you agree, then stores them with `set_preferences`).

```yaml
# EXAMPLE ONLY: tag names here are made up
featureImport:
  tagRules:
    - tag: group                 # key form: matches @group=alpha and @group:alpha (key is case-insensitive)
      split: ","                 # @group=alpha,beta gives the values alpha and beta
      testSet: "{value}"         # one Test Set per value (reused by exact name, else created)
    - tag: team
      field: { component: "{value}" }   # friendly field name from projects.<KEY>.fields.test
      label: "team-{value}"
    - match: "(wip|draft)"       # regex form: the whole tag (without @) must match
      label: "to-review"         # only {tag} and $1..$9 are available here
    - tag: internal
      skip: true                 # drop the tag; skip cannot be combined with other actions
```

- Key form (`tag`, a string or a list): matches `key=value` and `key:value`. Actions: `label`, `testSet` and `field` take templates (`{value}`, `{key}`, `{tag}`); `split` splits the value into several.
- Regex form (`match`): matches the whole tag; templates may use only `{tag}` and `$1`..`$9` (`{value}` and `{key}` are refused).
- The first matching rule wins. Project `tagRules` replace the top-level list; remembered rules (answers to `tagRule.<key>` questions or `set_preferences`) come after the settings rules.
- `tagsAsLabels` only concerns plain tags no rule covers: `false` drops them. A rule's `label`, `testSet` and `field` actions always apply.
- A rule that would produce an invalid label or a conflicting field value refuses the whole import before anything is written.
- Tags no rule covers appear in the preview's `tagSummary.unmatched`.

### notDone

Some things cannot be done without Jira: labels, fields or requirement links on existing tests, when there is no Jira token. A field that already has a different value is never overwritten either (an equal value is left alone silently), and a field whose current value could not be read is never written. The import still does everything else and lists the rest in `notDone: [{item, what, reason, fix}]`. When the reason says Jira access is missing ("no Jira token or Atlassian MCP") or not configured ("Jira access not configured: ... missing", when only some of the site URL, email and token are set), Claude can complete the items with the Atlassian MCP if it is connected, or you can set the Jira token (see "Optional Jira token" in [configuration.md](configuration.md#optional-jira-token)). Tools that need the Jira token for the whole operation (`edit_test` Jira fields, `set_lifecycle_label`) return an error with the same `reason` and `fix`.
